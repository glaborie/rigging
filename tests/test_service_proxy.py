"""X-Forwarded-For trust for the rate limiter (ADR-0021).

The app keys the limiter on `request.client.host`. The ASGI server is what turns
`X-Forwarded-For` into that value, and only when the immediate peer is in the
trusted-proxy list. These tests run a real uvicorn server so they assert that
wiring, not a mock: a trusted proxy gets per-client buckets, an untrusted peer
cannot choose its own bucket by sending the header.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from rigging.events import EventLog
from rigging.service import create_app

uvicorn = pytest.importorskip("uvicorn", reason="needs the [service] extra")


def _finished_run(tmp_path) -> str:
    log = EventLog(tmp_path / "runs.db")
    session_id = log.start_session(title="proxy")
    run_id = log.start_run(session_id=session_id, model="fake", spec_hash="x")
    log.append(run_id, "run_finished", {"status": "ok"})
    log.finish_run(run_id, "ok")
    return str(log.path)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """One uvicorn process on a loopback socket, configured like `harness serve`."""

    def __init__(self, db: str, trusted: str) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(db, rate_limit_per_minute=1),
                host="127.0.0.1",
                port=_free_port(),
                log_level="error",
                proxy_headers=True,
                forwarded_allow_ips=trusted,
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                port = self._server.servers[0].sockets[0].getsockname()[1]
                return f"http://127.0.0.1:{port}"
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _get(base: str, client_ip: str) -> int:
    with httpx.Client(trust_env=False) as client:
        response = client.get(f"{base}/api/runs", headers={"X-Forwarded-For": client_ip})
    return response.status_code


def test_a_trusted_proxy_gets_one_bucket_per_client(tmp_path):
    # The test connects from 127.0.0.1, which is trusted here, so uvicorn adopts
    # the forwarded address as the client.
    with _Server(_finished_run(tmp_path), "127.0.0.1,::1") as base:
        assert _get(base, "203.0.113.7") == 200
        assert _get(base, "203.0.113.7") == 429, "the same client exhausts its bucket"
        assert _get(base, "203.0.113.8") == 200, "another client has its own bucket"


def test_an_untrusted_peer_cannot_choose_its_bucket(tmp_path):
    # 127.0.0.1 is not in this list, so the header is ignored: both requests are
    # keyed by the socket peer and share one bucket. Spoofing buys nothing.
    with _Server(_finished_run(tmp_path), "10.9.9.9") as base:
        assert _get(base, "203.0.113.7") == 200
        assert _get(base, "203.0.113.8") == 429
