"""The write path (ADR-0023): one `run_agent` per turn, approvals from the page.

Offline by rule: TestClient runs the app in-process and the client is a
`FakeClient`, so no socket and no model is involved.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import yaml

from rigging.events import EventLog
from rigging.manifest import load_manifest
from rigging.registry import Registry
from rigging.testing import FakeClient

fastapi = pytest.importorskip("fastapi", reason="needs the [service] extra")

from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)

from rigging.service import ChatRunner, create_app  # noqa: E402

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _spec(tmp_path, **extra):
    data = {"name": "web-agent", "model": "fake", "system_prompt": "be terse",
            "limits": {"max_steps": 4}, "tools": ["refund"],
            # `cli` is the interactive policy the page stands in for; the runner
            # only replaces that one (see test_manifest_auto_deny_is_respected).
            "approval": {"policy": "cli", "side_effecting_tools": ["refund"]}}
    data.update(extra)
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_manifest(str(path))


def _registry() -> Registry:
    """One side-effecting tool, so an approval has something to gate."""
    reg = Registry()

    def refund(amount: int) -> str:
        """Refund money."""
        return f"refunded {amount}"

    refund.__rigging_tool__ = {"name": "refund", "description": "Refund money.",
                               "side_effecting": True}
    reg.add("tool", "refund", refund)
    return reg


def _app(tmp_path, client: Any, *, spec=None, **kwargs):
    db = tmp_path / "web.db"
    EventLog(db).close()  # create_app refuses a missing log, like `harness serve`
    runner = ChatRunner(spec or _spec(tmp_path), str(db),
                        client_factory=lambda: client, registry=_registry())
    return TestClient(create_app(db, tokens=TOKEN, runner=runner, **kwargs)), db


def _wait(client: TestClient, run_id: str, type_: str, timeout: float = 10.0) -> dict[str, Any]:
    """Poll the log until the event shows up; the turn runs in a thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/runs/{run_id}", headers=AUTH).json()
        for ev in body["events"]:
            if ev["type"] == type_:
                return ev
        if body["run"]["status"] not in ("running", None) and type_ == "run_finished":
            return {"type": "run_finished", "payload": {"status": body["run"]["status"]}}
        time.sleep(0.05)
    raise AssertionError(f"{type_} never appeared for {run_id}")


def test_a_turn_starts_a_run_the_page_can_stream(tmp_path):
    client = FakeClient([{"role": "assistant", "content": "hello back"}])
    api, _ = _app(tmp_path, client)

    started = api.post("/api/chat", json={"prompt": "hi"}, headers=AUTH)
    assert started.status_code == 202, started.text
    ids = started.json()
    assert ids["session_id"].startswith("s_") and ids["run_id"].startswith("r_")

    # The run row exists before the first event, so the page never races the
    # thread: this GET happens immediately after the POST returns.
    run = api.get(f"/api/runs/{ids['run_id']}", headers=AUTH).json()["run"]
    assert run["session_id"] == ids["session_id"]

    _wait(api, ids["run_id"], "run_finished")
    events = api.get(f"/api/runs/{ids['run_id']}", headers=AUTH).json()["events"]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_started"
    assert "input_received" in kinds and "model_response" in kinds
    assert client.requests[0].messages[-1].content == "hi"


def test_approval_is_answered_from_the_page(tmp_path):
    client = FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "done"},
    ])
    api, _ = _app(tmp_path, client)
    run_id = api.post("/api/chat", json={"prompt": "refund 10"}, headers=AUTH).json()["run_id"]

    asked = _wait(api, run_id, "approval_requested")
    assert asked["payload"]["tool"] == "refund"

    answered = api.post(f"/api/runs/{run_id}/approval", json={"approve": True}, headers=AUTH)
    assert answered.status_code == 200 and answered.json()["approved"] is True

    verdict = _wait(api, run_id, "approval_verdict")
    assert verdict["payload"]["approved"] is True
    result = _wait(api, run_id, "tool_result")
    assert result["payload"]["denied"] is False
    assert "refunded 10" in result["payload"]["content"]


def test_denied_approval_blocks_the_tool(tmp_path):
    client = FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "I could not refund it"},
    ])
    api, _ = _app(tmp_path, client)
    run_id = api.post("/api/chat", json={"prompt": "refund 10"}, headers=AUTH).json()["run_id"]
    _wait(api, run_id, "approval_requested")

    api.post(f"/api/runs/{run_id}/approval", json={"approve": False}, headers=AUTH)

    result = _wait(api, run_id, "tool_result")
    assert result["payload"]["denied"] is True
    assert "denied by approval policy" in result["payload"]["content"]


def test_approval_for_an_idle_run_is_a_conflict(tmp_path):
    api, _ = _app(tmp_path, FakeClient([{"role": "assistant", "content": "hi"}]))
    run_id = api.post("/api/chat", json={"prompt": "hi"}, headers=AUTH).json()["run_id"]
    _wait(api, run_id, "run_finished")
    res = api.post(f"/api/runs/{run_id}/approval", json={"approve": True}, headers=AUTH)
    assert res.status_code == 409


def test_a_second_turn_rebuilds_history_from_the_log(tmp_path):
    client = FakeClient([
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "again"},
    ])
    api, _ = _app(tmp_path, client)

    first = api.post("/api/chat", json={"prompt": "first question"}, headers=AUTH).json()
    _wait(api, first["run_id"], "run_finished")
    second = api.post("/api/chat",
                      json={"prompt": "second question", "session_id": first["session_id"]},
                      headers=AUTH).json()
    assert second["session_id"] == first["session_id"]
    _wait(api, second["run_id"], "run_finished")

    # The second call is a fresh run_agent, so the history it was given must come
    # from the log: both prompts, in order, with the first answer between them.
    roles = [(m.role, m.content) for m in client.requests[1].messages]
    assert ("user", "first question") in roles
    assert ("assistant", "hello") in roles
    assert roles[-1] == ("user", "second question")


def test_a_turn_is_refused_while_the_session_is_busy(tmp_path):
    api, db = _app(tmp_path, FakeClient([{"role": "assistant", "content": "hi"}]))
    log = EventLog(db)
    sid = log.start_session(title="busy")
    log.start_run(session_id=sid, model="fake")
    log.close()

    res = api.post("/api/chat", json={"prompt": "hi", "session_id": sid}, headers=AUTH)
    assert res.status_code == 409
    assert "still running" in res.json()["detail"]


def test_chat_refuses_to_exist_without_a_token(tmp_path):
    db = tmp_path / "web.db"
    EventLog(db).close()
    runner = ChatRunner(_spec(tmp_path), str(db), client_factory=lambda: None)
    with pytest.raises(ValueError, match="RIGGING_SERVICE_TOKEN"):
        create_app(db, runner=runner)


def test_chat_needs_the_bearer_token(tmp_path):
    api, _ = _app(tmp_path, FakeClient([{"role": "assistant", "content": "hi"}]))
    assert api.post("/api/chat", json={"prompt": "hi"}).status_code == 401
    assert api.get("/api/config").status_code == 401


def test_read_only_app_has_no_write_routes(tmp_path):
    """Without a runner the service is exactly what ADR-0018 shipped."""
    db = tmp_path / "web.db"
    log = EventLog(db)
    log.start_session(title="t")
    log.close()
    api = TestClient(create_app(db, tokens=TOKEN))

    assert api.post("/api/chat", json={"prompt": "hi"}, headers=AUTH).status_code in (404, 405)
    assert api.get("/api/config", headers=AUTH).json()["chat"] is False


def test_config_tells_the_page_whether_it_can_chat(tmp_path):
    api, _ = _app(tmp_path, FakeClient([{"role": "assistant", "content": "hi"}]))
    config = api.get("/api/config", headers=AUTH).json()
    assert config == {"chat": True, "manifest": "web-agent", "model": "fake",
                      "approvals": "page"}


def test_index_has_the_chat_surface():
    from rigging.service.app import INDEX_HTML

    assert "/api/chat" in INDEX_HTML
    assert "/approval" in INDEX_HTML
    assert "read-only view of the event log" in INDEX_HTML


def test_session_endpoint_returns_every_turn_in_order(tmp_path):
    client = FakeClient([
        {"role": "assistant", "content": "one"},
        {"role": "assistant", "content": "two"},
    ])
    api, _ = _app(tmp_path, client)
    first = api.post("/api/chat", json={"prompt": "first"}, headers=AUTH).json()
    _wait(api, first["run_id"], "run_finished")
    api.post("/api/chat", json={"prompt": "second", "session_id": first["session_id"]},
             headers=AUTH)

    body = api.get(f"/api/sessions/{first['session_id']}", headers=AUTH).json()
    assert next(t["run"]["run_id"] for t in body["turns"]) == first["run_id"]
    assert len(body["turns"]) == 2
    prompts = [e["payload"]["content"] for t in body["turns"] for e in t["events"]
               if e["type"] == "input_received"]
    assert prompts == ["first", "second"]
    assert api.get("/api/sessions/s_nope", headers=AUTH).status_code == 404


def test_manifest_auto_deny_is_respected(tmp_path):
    """A server flag must not widen an explicit unattended policy to the page."""
    spec = _spec(tmp_path, approval={"policy": "auto_deny",
                                     "side_effecting_tools": ["refund"]})
    client = FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "no refund"},
    ])
    api, _ = _app(tmp_path, client, spec=spec)
    assert api.get("/api/config", headers=AUTH).json()["approvals"] == "auto_deny"

    run_id = api.post("/api/chat", json={"prompt": "refund 10"}, headers=AUTH).json()["run_id"]
    result = _wait(api, run_id, "tool_result")
    assert result["payload"]["denied"] is True
    assert "no approver attached" in result["payload"]["content"]
    # Nothing was waiting for the page, so there is nothing to answer.
    assert api.post(f"/api/runs/{run_id}/approval", json={"approve": True},
                    headers=AUTH).status_code == 409
