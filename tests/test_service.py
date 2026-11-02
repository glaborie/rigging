"""The read-only widget (ADR-0018): same log, no second store, no writes.

Offline by rule: TestClient runs the app in-process, so no socket is opened.
"""

from __future__ import annotations

import json

import pytest

from rigging.events import EventLog

fastapi = pytest.importorskip("fastapi", reason="needs the [service] extra")

from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)

from rigging.service import create_app  # noqa: E402


def _finished_run(tmp_path, *, status: str = "ok") -> tuple[object, str]:
    log = EventLog(tmp_path / "runs.db")
    session_id = log.start_session(title="demo agent")
    run_id = log.start_run(session_id=session_id, model="fake", spec_hash="abc")
    log.append(run_id, "input_received", {"content": "hello"})
    log.append(run_id, "model_response", {"content": "hi", "usage": {"input_tokens": 3}})
    log.append(run_id, "run_finished", {"status": status})
    log.finish_run(run_id, status)
    return log, run_id


def _client(tmp_path, log, **kwargs) -> TestClient:
    return TestClient(create_app(log.path, **kwargs))


def test_index_serves_the_page(tmp_path):
    log, _ = _finished_run(tmp_path)
    body = _client(tmp_path, log).get("/").text
    assert "EventSource" in body and "/api/runs" in body
    assert "read-only view of the event log" in body


def test_runs_endpoint_lists_the_run_with_its_session(tmp_path):
    log, run_id = _finished_run(tmp_path)
    payload = _client(tmp_path, log).get("/api/runs").json()
    assert [r["run_id"] for r in payload["runs"]] == [run_id]
    assert payload["runs"][0]["title"] == "demo agent"
    assert payload["runs"][0]["status"] == "ok"


def test_events_endpoint_is_the_log_in_order(tmp_path):
    log, run_id = _finished_run(tmp_path)
    payload = _client(tmp_path, log).get(f"/api/runs/{run_id}").json()
    types = [e["type"] for e in payload["events"]]
    assert types[:2] == ["run_started", "input_received"]
    assert types[-1] == "run_finished"
    assert payload["events"][1]["payload"]["content"] == "hello"


def test_stream_replays_a_finished_run_then_ends(tmp_path):
    log, run_id = _finished_run(tmp_path)
    response = _client(tmp_path, log).get(f"/api/runs/{run_id}/stream")
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.splitlines() if line.startswith("data: ")]
    types = [json.loads(f[len("data: "):]).get("type") for f in frames]
    assert "run_started" in types and "run_finished" in types
    assert "event: end" in response.text


def test_stream_stops_on_an_idle_running_run(tmp_path):
    log, run_id = _finished_run(tmp_path, status="running")
    response = _client(tmp_path, log, idle_seconds=0.05).get(f"/api/runs/{run_id}/stream")
    assert '"reason": "idle"' in response.text


def test_child_runs_are_visible_with_their_parent(tmp_path):
    log, parent = _finished_run(tmp_path)
    session_id = log.runs()[0]["session_id"]
    child = log.start_run(session_id=session_id, model="fake", parent_run_id=parent)
    log.append(child, "run_finished", {"status": "ok"})
    log.finish_run(child, "ok")

    payload = _client(tmp_path, log).get("/api/runs").json()
    by_id = {r["run_id"]: r for r in payload["runs"]}
    assert by_id[child]["parent_run_id"] == parent


def test_unknown_run_is_a_404(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = _client(tmp_path, log)
    assert client.get("/api/runs/r_nope").status_code == 404
    assert client.get("/api/runs/r_nope/stream").status_code == 404


def test_the_service_never_writes(tmp_path):
    """The log is opened read-only, so a write attempt fails instead of drifting."""
    log, run_id = _finished_run(tmp_path)
    app = create_app(log.path)
    assert len(app.routes) == 4  # /, /api/runs, /api/runs/{id}, /api/runs/{id}/stream
    before = (tmp_path / "runs.db").read_bytes()
    TestClient(app).get(f"/api/runs/{run_id}")
    assert (tmp_path / "runs.db").read_bytes() == before


def test_missing_database_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="no event log"):
        create_app(tmp_path / "absent.db")
