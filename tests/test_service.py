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
    assert "/api/runs" in body and "Authorization" in body
    assert "read-only view of the event log" in body


def test_index_javascript_keeps_its_escaped_newlines():
    """A Python `\n` in a non-raw string becomes a real newline in the served JS.

    That puts a line break inside a single-quoted JS string, the whole script
    fails to parse, and the page sits on "loading runs…" forever. This is the
    regression test for that class of bug.
    """
    from rigging.service.app import INDEX_HTML

    assert "indexOf('\\n\\n')" in INDEX_HTML
    assert "split('\\n')" in INDEX_HTML
    assert "data.join('\\n')" in INDEX_HTML


def test_runs_endpoint_lists_the_run_with_its_session(tmp_path):
    log, run_id = _finished_run(tmp_path)
    payload = _client(tmp_path, log).get("/api/runs").json()
    assert [r["run_id"] for r in payload["runs"]] == [run_id]
    assert payload["runs"][0]["title"] == "demo agent"
    assert payload["runs"][0]["status"] == "ok"


def test_escalations_endpoint_lists_handoffs(tmp_path):
    log, run_id = _finished_run(tmp_path)
    log.append(run_id, "escalation", {
        "reason": "customer_requested", "summary": "needs a human",
        "facts": {"order_id": "ORD-1002"}, "transcript_run_id": run_id,
    })
    payload = _client(tmp_path, log).get("/api/escalations").json()["escalations"]
    assert payload[0]["run_id"] == run_id
    assert payload[0]["reason"] == "customer_requested"
    assert payload[0]["facts"] == {"order_id": "ORD-1002"}
    assert payload[0]["session_title"] == "demo agent"


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
    types = [json.loads(f[len("data: ") :]).get("type") for f in frames]
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
    """Without a manifest the log is read-only: every route is a GET (ADR-0018)."""
    log, run_id = _finished_run(tmp_path)
    app = create_app(log.path)
    assert {route.path for route in app.routes if hasattr(route, "methods")} == {
        "/", "/api/runs", "/api/escalations", "/api/config", "/api/runs/{run_id}",
        "/api/sessions/{session_id}", "/api/runs/{run_id}/stream",
    }
    assert all(route.methods == {"GET"} for route in app.routes if hasattr(route, "methods"))
    before = (tmp_path / "runs.db").read_bytes()
    TestClient(app).get(f"/api/runs/{run_id}")
    assert (tmp_path / "runs.db").read_bytes() == before


def test_missing_database_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="no event log"):
        create_app(tmp_path / "absent.db")


# ------------------------------------------------------------------- auth (ADR-0021)


def test_the_shell_is_public_but_the_data_is_not(tmp_path):
    log, run_id = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, tokens=["secret"]))

    assert client.get("/").status_code == 200, "the page must load so it can ask for a token"
    assert client.get("/api/runs").status_code == 401
    assert client.get(f"/api/runs/{run_id}").status_code == 401
    assert client.get(f"/api/runs/{run_id}/stream").status_code == 401
    assert client.get("/api/runs").headers["www-authenticate"] == "Bearer"


def test_a_valid_bearer_token_is_accepted(tmp_path):
    log, run_id = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, tokens=["secret"]))
    headers = {"Authorization": "Bearer secret"}

    assert client.get("/api/runs", headers=headers).status_code == 200
    assert client.get(f"/api/runs/{run_id}", headers=headers).status_code == 200
    stream = client.get(f"/api/runs/{run_id}/stream", headers=headers)
    assert stream.status_code == 200 and "event: end" in stream.text


def test_a_wrong_token_is_rejected(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, tokens=["secret"]))
    assert client.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/runs", headers={"Authorization": "secret"}).status_code == 401


def test_tokens_rotate_by_comma(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, tokens="old, new"))
    for token in ("old", "new"):
        assert (
            client.get("/api/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        )


def test_no_token_means_no_auth_on_loopback(tmp_path):
    log, _ = _finished_run(tmp_path)
    assert TestClient(create_app(log.path)).get("/api/runs").status_code == 200


def test_exposing_the_service_without_a_token_is_refused():
    from rigging.service import check_bind

    with pytest.raises(ValueError, match="RIGGING_SERVICE_TOKEN"):
        check_bind("0.0.0.0", [])
    # "" is not loopback: uvicorn would bind every interface.
    with pytest.raises(ValueError, match="RIGGING_SERVICE_TOKEN"):
        check_bind("", [])
    check_bind("127.0.0.1", [])
    check_bind("127.0.0.2", [])
    check_bind("::1", [])
    check_bind("localhost", [])


def test_a_token_over_plain_http_is_refused_off_loopback():
    """A bearer token on the wire is only safe behind TLS (docs/threat-model.md)."""
    from rigging.service import check_bind

    with pytest.raises(ValueError, match="plain HTTP"):
        check_bind("0.0.0.0", ["secret"])
    # The two documented escapes: TLS on the socket, or an explicit opt-in when
    # the network already protects the link.
    check_bind("0.0.0.0", ["secret"], tls=True)
    check_bind("0.0.0.0", ["secret"], allow_plaintext=True)
    # Loopback needs neither.
    check_bind("127.0.0.1", ["secret"])


def test_a_trusted_proxy_list_that_trusts_everyone_is_refused():
    """`*` makes X-Forwarded-For spoofable, so it is a startup error, not a warning."""
    from rigging.service import check_trusted_proxies

    for value in ("*", "127.0.0.1,*", "0.0.0.0/0", "::/0"):
        with pytest.raises(ValueError, match="trust"):
            check_trusted_proxies(value)
    check_trusted_proxies("127.0.0.1,::1")
    check_trusted_proxies("10.0.0.0/8")
    check_trusted_proxies("")  # trust nobody; the socket peer is the key


# ------------------------------------------------------- headers and rate limit


def test_security_headers_and_a_matching_csp_nonce(tmp_path):
    log, _ = _finished_run(tmp_path)
    response = _client(tmp_path, log).get("/")
    headers = response.headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp

    # The nonce in the policy must be the one on the inline script and style,
    # or the browser refuses to run the page.
    nonce = csp.split("script-src 'nonce-", 1)[1].split("'", 1)[0]
    body = response.text
    assert f'<script nonce="{nonce}">' in body
    assert f'<style nonce="{nonce}">' in body
    assert "__CSP_NONCE__" not in body


def test_the_nonce_changes_per_response(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = _client(tmp_path, log)
    first = client.get("/").headers["content-security-policy"]
    second = client.get("/").headers["content-security-policy"]
    assert first != second


def test_api_requests_are_rate_limited_per_client(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, rate_limit_per_minute=2))

    assert client.get("/api/runs").status_code == 200
    assert client.get("/api/runs").status_code == 200
    limited = client.get("/api/runs")
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    # The shell stays reachable: the page can explain the 429 instead of a blank tab.
    assert client.get("/").status_code == 200


def test_the_limit_is_checked_before_the_token(tmp_path):
    """A token-guessing script spends its own budget, not free comparisons."""
    log, _ = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, tokens=["secret"], rate_limit_per_minute=2))

    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs").status_code == 429


def test_rate_limiting_can_be_disabled(tmp_path):
    log, _ = _finished_run(tmp_path)
    client = TestClient(create_app(log.path, rate_limit_per_minute=0))
    assert all(client.get("/api/runs").status_code == 200 for _ in range(10))
