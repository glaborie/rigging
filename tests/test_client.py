"""Wire-level contract tests for the SSE parser.

Hand-rolled streaming parsers are where bugs live: these fixtures are the reason
the parser is trustworthy without a live gateway.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from rigging.client.openai_compat import OpenAICompatClient
from rigging.types import Message, ModelRequest

FIXTURES = Path(__file__).parent / "fixtures"


def _client(body: str, status: int = 200) -> OpenAICompatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode(),
                              headers={"content-type": "text/event-stream"})

    return OpenAICompatClient("http://gw/v1", "k", transport=httpx.MockTransport(handler))


def _request() -> ModelRequest:
    return ModelRequest(model="test-model", messages=[Message(role="user", content="hi")])


def test_stream_text_reasoning_and_usage() -> None:
    client = _client((FIXTURES / "sse_text.txt").read_text(encoding="utf-8"))
    deltas = list(client.stream(_request()))
    assert "".join(d.text for d in deltas) == "Hello world"
    assert "".join(d.reasoning for d in deltas).startswith("thinking")
    assert deltas[-1].usage is not None
    assert deltas[-1].usage.output_tokens == 7


def test_tool_call_deltas_are_reassembled() -> None:
    client = _client((FIXTURES / "sse_tool_calls.txt").read_text(encoding="utf-8"))
    response = client.complete(_request())
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "search_policies"
    assert call.arguments == {"query": "return window", "k": 3}
    assert response.finish_reason == "tool_calls"


def test_malformed_event_is_reported_not_fatal() -> None:
    body = (FIXTURES / "sse_malformed.txt").read_text(encoding="utf-8")
    seen: list[tuple[str, dict]] = []
    client = _client(body)
    deltas = list(client.stream(_request(), on_error=lambda t, p: seen.append((t, p))))
    assert "".join(d.text for d in deltas) == "still works"
    assert seen and seen[0][0] == "sse_malformed"


def test_bad_arguments_do_not_raise() -> None:
    body = (FIXTURES / "sse_bad_args.txt").read_text(encoding="utf-8")
    response = _client(body).complete(_request())
    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].raw_arguments == "{not json"


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, content=b"slow down")
        return httpx.Response(200, content=(FIXTURES / "sse_text.txt").read_text(encoding="utf-8").encode())

    client = OpenAICompatClient("http://gw/v1", "k", max_retries=2,
                                transport=httpx.MockTransport(handler))
    deltas = list(client.stream(_request()))
    assert calls["n"] == 2
    assert "".join(d.text for d in deltas) == "Hello world"
