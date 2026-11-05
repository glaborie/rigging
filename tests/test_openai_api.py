"""The OpenAI-compatible surface for a chat UI (ADR-0028).

Offline by rule: `TestClient` runs the app in-process and `FakeClient` replays
scripted responses, so nothing here opens a socket or calls a model. What is under
test is the translation — OpenAI request in, one `run_agent` turn, OpenAI response
out — and the three things the client cannot do for itself: resolve a placeholder,
approve a refund, or report a tool call.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from rigging.events import EventLog
from rigging.manifest import load_manifest
from rigging.protocols import Allow, Deny
from rigging.registry import Registry
from rigging.testing import FakeClient
from rigging.types import Message, ModelDelta, ModelResponse, RunResult, Usage

fastapi = pytest.importorskip("fastapi", reason="needs the [service] extra")

from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)

from rigging.service import create_app  # noqa: E402
from rigging.service.openai_api import (  # noqa: E402
    ChatCompletionRequest,
    OpenAIBridge,
    PreparedTurn,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "examples/b2b-support/agent.yaml"
PLAIN = ROOT / "examples/hello/agent.yaml"  # no output guardrails: the live path
TOKEN = "secret-token"
CONVERSATION = "conv-abc"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Conversation-Id": CONVERSATION}


def _bridge(tmp_path, responses, **spec_overrides) -> OpenAIBridge:
    spec = load_manifest(str(MANIFEST))
    if spec_overrides:
        spec = spec.model_copy(update=spec_overrides)
    client = FakeClient(responses)
    return OpenAIBridge(spec, str(tmp_path / "runs.db"),
                        client_factory=lambda: client)


def _app(tmp_path, bridge: OpenAIBridge, **kwargs) -> TestClient:
    EventLog(tmp_path / "runs.db").close()  # create_app insists the log exists
    return TestClient(create_app(tmp_path / "runs.db", tokens=[TOKEN], bridge=bridge,
                                **kwargs))


def _completion(messages, *, stream=False, model="b2b-support", **extra):
    return {"model": model, "messages": messages, "stream": stream, **extra}


# ------------------------------------------------------------------ the surface


def test_models_lists_exactly_one_agent_and_needs_the_token(tmp_path):
    bridge = _bridge(tmp_path, [])
    client = _app(tmp_path, bridge)
    assert client.get("/v1/models").status_code == 401
    body = client.get("/v1/models", headers=HEADERS).json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == [bridge.model_id]


def test_a_completion_returns_the_agents_answer(tmp_path):
    bridge = _bridge(tmp_path, [{"content": "ORD-1002 is in transit."}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "user", "content": "where is my order?"}]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant",
                                             "content": "ORD-1002 is in transit."}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 15
    assert body["rigging"]["status"] == "ok"


def test_the_manifest_owns_the_system_prompt(tmp_path):
    """A client-sent system message is dropped: the manifest is the prompt."""
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "system", "content": "You are a pirate."},
        {"role": "user", "content": "hello"},
    ]))
    sent = bridge._client_factory().requests[0]
    system = [m for m in sent.messages if m.role == "system"]
    assert len(system) == 1 and "pirate" not in system[0].content


def test_the_conversation_id_is_required_and_the_refusal_says_where_to_set_it(tmp_path):
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions",
                           headers={"Authorization": f"Bearer {TOKEN}"},
                           json=_completion([{"role": "user", "content": "hi"}]))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "LIBRECHAT_BODY_CONVERSATIONID" in error["message"]


def test_an_unknown_model_is_a_404_not_a_silent_answer(tmp_path):
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "user", "content": "hi"}],
                                            model="gpt-something-else"))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"


def test_no_user_message_is_a_400(tmp_path):
    bridge = _bridge(tmp_path, [])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "assistant", "content": "hi"}]))
    assert response.status_code == 400
    assert "no user message" in response.json()["error"]["message"]


def test_a_conversation_over_the_request_cap_is_refused(tmp_path):
    bridge = _bridge(tmp_path, [])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion(
        [{"role": "user", "content": "hi"} for _ in range(201)]))
    assert response.status_code == 400
    assert "at most 200" in response.json()["error"]["message"]


# ------------------------------------------------------------------- the session


def test_two_requests_in_one_conversation_are_one_session(tmp_path):
    """The header is what lets turn 2 resolve the `[EMAIL_1]` from turn 1."""
    bridge = _bridge(tmp_path, [{"content": "first"}, {"content": "second"}])
    client = _app(tmp_path, bridge)
    for prompt in ("one", "two"):
        assert client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "user", "content": prompt}])
                           ).status_code == 200
    log = EventLog(tmp_path / "runs.db")
    try:
        sessions = {row["session_id"] for row in log.runs()}
        assert len(sessions) == 1
        assert sessions == {bridge.session_id(CONVERSATION)}
        # The turn is in the log like any other run: a reviewer reads it there.
        kinds = [ev["type"] for ev in log.events(log.runs()[0]["run_id"])]
        assert "input_received" in kinds and "run_finished" in kinds
    finally:
        log.close()


def test_a_second_turn_while_one_is_running_is_409(tmp_path):
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    log = EventLog(tmp_path / "runs.db")
    try:
        started = log.start_session(bridge.session_id(CONVERSATION), title="in flight")
        log.start_run(session_id=started, model="fake")  # left running on purpose
    finally:
        log.close()
    response = client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "user", "content": "hi"}]))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "turn_in_progress"


def test_history_from_the_client_is_sent_as_history(tmp_path):
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]))
    sent = bridge._client_factory().requests[0]
    assert [(m.role, m.content) for m in sent.messages][1:] == [
        ("user", "first"), ("assistant", "reply"), ("user", "second")]


def test_pii_in_the_clients_history_uses_the_session_sentinels(tmp_path):
    """The client keeps the user's raw text; replaying it would undo ADR-0025."""
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": "email jane@example.com"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "and the order?"},
    ]))
    assert response.status_code == 200
    sent = bridge._client_factory().requests[0]
    content = "\n".join(m.content for m in sent.messages)
    assert "jane@example.com" not in content
    assert "[EMAIL_" in content


def test_history_without_a_pii_guardrail_gets_the_redaction_floor(tmp_path):
    bridge = _plain_bridge(tmp_path, FakeClient([{"content": "ok"}]))
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": "email jane@example.com"},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "and the order?"},
    ], model="hello"))
    assert response.status_code == 200
    sent = bridge._client_factory().requests[0]
    content = "\n".join(m.content for m in sent.messages)
    assert "jane@example.com" not in content
    assert "[REDACTED]" in content


def test_two_identical_prompts_keep_the_turn_between_them(tmp_path):
    """Finding the last user message by equality would return the first "same"."""
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "same"},
    ]))
    assert response.status_code == 200
    sent = bridge._client_factory().requests[0]
    assert [(m.role, m.content) for m in sent.messages][1:] == [
        ("user", "same"), ("assistant", "reply"), ("user", "same")]


def test_content_parts_are_flattened_to_text(tmp_path):
    """OpenAI's multi-part message shape is accepted when every part is text."""
    bridge = _bridge(tmp_path, [{"content": "ok"}])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]},
    ]))
    assert response.status_code == 200
    sent = bridge._client_factory().requests[0]
    assert sent.messages[-1].content == "hello\nworld"


def test_an_image_part_is_refused_not_dropped(tmp_path):
    bridge = _bridge(tmp_path, [])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS, json=_completion([
        {"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]))
    assert response.status_code == 400
    assert "text messages only" in response.json()["error"]["message"]


def test_the_openai_surface_does_not_exist_without_a_bridge(tmp_path):
    """Without --openai-api the app is exactly the page's two modes."""
    db = tmp_path / "runs.db"
    EventLog(db).close()
    client = TestClient(create_app(db, tokens=[TOKEN]))
    assert client.get("/v1/models", headers=HEADERS).status_code in (404, 405)


def test_the_bridge_refuses_to_exist_without_a_token(tmp_path):
    bridge = _bridge(tmp_path, [])
    db = tmp_path / "runs.db"
    EventLog(db).close()
    with pytest.raises(ValueError, match="RIGGING_SERVICE_TOKEN"):
        create_app(db, bridge=bridge)


# ------------------------------------------------------------------- streaming


class StreamingFake:
    """A client whose `stream()` yields deltas, which `FakeClient` refuses to do."""

    name = "streaming-fake"

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.requests: list[object] = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResponse(message=Message(role="assistant", content="".join(self.chunks)),
                             usage=Usage(input_tokens=10, output_tokens=5),
                             finish_reason="stop", model="fake")

    def stream(self, request, on_error=None):
        self.requests.append(request)
        for chunk in self.chunks:
            yield ModelDelta(text=chunk)
        # What the real client sends on the last frame: `stream_options`
        # include_usage is a gateway feature, not something this server invents.
        yield ModelDelta(finish_reason="stop",
                         usage=Usage(input_tokens=10, output_tokens=5))


def _plain_bridge(tmp_path, client) -> OpenAIBridge:
    """The hello manifest has no output guardrails, so streaming is live."""
    return OpenAIBridge(load_manifest(str(PLAIN)), str(tmp_path / "runs.db"),
                        client_factory=lambda: client)


def _frames(response) -> list[dict]:
    data = [line[len("data: "):] for line in response.iter_lines()
            if line.startswith("data: ")]
    assert data[-1] == "[DONE]"
    return [json.loads(item) for item in data[:-1]]


def test_streaming_yields_openai_chunks_and_done(tmp_path):
    bridge = _plain_bridge(tmp_path, StreamingFake(["hello", " there"]))
    client = _app(tmp_path, bridge)
    with client.stream("POST", "/v1/chat/completions", headers=HEADERS,
                       json=_completion([{"role": "user", "content": "hi"}],
                                        stream=True, model="hello")) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = _frames(response)

    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[1:-1])
    assert text == "hello there"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_streaming_honours_include_usage(tmp_path):
    bridge = _plain_bridge(tmp_path, StreamingFake(["ok"]))
    client = _app(tmp_path, bridge)
    with client.stream("POST", "/v1/chat/completions", headers=HEADERS,
                       json=_completion([{"role": "user", "content": "hi"}], stream=True,
                                        model="hello",
                                        stream_options={"include_usage": True})) as response:
        payloads = _frames(response)
    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    final = payloads[-1]
    assert final["choices"] == []
    assert final["usage"]["total_tokens"] == 15


def test_a_stream_without_deltas_still_delivers_the_answer(tmp_path):
    """A denial, or a final answer from a non-streamed call, is not an empty bubble."""
    bridge = _plain_bridge(tmp_path, None)
    bridge._turn = lambda *args, **kwargs: RunResult(  # type: ignore[method-assign]
        run_id="r_x", status="ok", output="the answer")
    frames = [json.loads(frame[len("data: "):])
              for frame in bridge.stream(
                  PreparedTurn("hi", [], "lc_x"),
                  ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}]))
              if frame.startswith("data: ") and frame != "data: [DONE]\n\n"]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in frames
                   if c["choices"])
    assert text == "the answer"


def test_a_budget_final_answer_is_not_lost_after_streamed_text(tmp_path):
    """The exhaustion answer is a second, non-streamed call; deliver it too."""
    bridge = _plain_bridge(tmp_path, None)

    def fake_turn(prompt, history, session_id, stream=None):
        stream(ModelDelta(text="checking… "))
        return RunResult(run_id="r_x", status="budget_exhausted",
                         output="here is what I have")

    bridge._turn = fake_turn  # type: ignore[method-assign]
    frames = [json.loads(frame[len("data: "):])
              for frame in bridge.stream(
                  PreparedTurn("hi", [], "lc_x"),
                  ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}]))
              if frame.startswith("data: ") and frame != "data: [DONE]\n\n"]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in frames
                   if c["choices"])
    assert text == "checking… here is what I have"
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "length"
               for c in frames)


def test_streaming_waits_for_output_guardrails(tmp_path):
    """An answer the policy is about to deny must not reach the stream first.

    A manifest with an `on_output` guardrail gets a buffered stream: the raw
    deltas would show the user a promise the harness then refused to make.
    """

    class Blocker:
        name = "blocker"
        hooks = ("on_output",)

        def on_input(self, messages):
            return Allow()

        def on_tool_call(self, call, tool):
            return Allow()

        def on_tool_result(self, call, content):
            return Allow()

        def on_output(self, text, messages):
            return Deny("no promises without a checked policy")

    spec_path = tmp_path / "blocker.yaml"
    spec_path.write_text(yaml.safe_dump({
        "name": "blocker-agent", "model": "fake", "system_prompt": "be terse",
        "limits": {"max_steps": 2},
        "guardrails": {"on_output": ["blocker"]},
    }))
    registry = Registry()
    registry.add("guardrail", "blocker", Blocker)
    bridge = OpenAIBridge(load_manifest(str(spec_path)), str(tmp_path / "runs.db"),
                          client_factory=lambda: FakeClient([
                              {"content": "I can refund the full amount today."}]),
                          registry=registry)
    client = _app(tmp_path, bridge)
    with client.stream("POST", "/v1/chat/completions", headers=HEADERS,
                       json=_completion([{"role": "user", "content": "can I get a refund?"}],
                                        stream=True, model="blocker-agent")) as response:
        payloads = _frames(response)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in payloads)
    assert "I can refund" not in text
    assert "[denied]" in text
    assert payloads[-1]["choices"][0]["finish_reason"] == "content_filter"


# ---------------------------------------------------------------- no approvals


def test_a_side_effecting_tool_is_denied_without_an_approval_channel(tmp_path, monkeypatch):
    """Hard rule 4 through the new surface: the loop denies, and says so.

    The manifest's policy is `cli`; serving it verbatim would block a request
    thread on a terminal prompt. The bridge passes `AutoDenyApproval` instead, so
    the refund is attempted, refused and recorded — and the answer is the denial,
    not a claim that money moved.
    """
    bridge = _bridge(tmp_path, [
        {"content": "", "tool_calls": [{"id": "c1", "name": "refund_order",
                                        "arguments": {"order_id": "ORD-1002",
                                                      "amount": 112.5,
                                                      "reason": "damaged"}}]},
        {"content": "I couldn't refund: the approval was denied."},
    ])
    client = _app(tmp_path, bridge)
    response = client.post("/v1/chat/completions", headers=HEADERS,
                           json=_completion([{"role": "user", "content": "refund ORD-1002"}]))
    assert response.status_code == 200
    assert "denied" in response.json()["choices"][0]["message"]["content"]

    log = EventLog(tmp_path / "runs.db")
    try:
        events = list(log.events(log.runs()[0]["run_id"]))
    finally:
        log.close()
    verdicts = [e["payload"] for e in events if e["type"] == "approval_verdict"]
    assert verdicts == [{"tool": "refund_order", "approved": False,
                         "note": "denied: no approver attached to this run (unattended)"}]
