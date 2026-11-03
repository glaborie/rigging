"""Headless-browser smoke test for the web view (ADR-0018).

Route tests read the page as text, so a JavaScript parse error is invisible to
them: v0.6.0 shipped a page that answered 200 and rendered nothing. This test
loads the real page in Chromium, waits for the run list, and fails on any page
error.

Opt-in (needs the [browser] extra and a Chromium build):

    uv sync --extra browser --extra service
    uv run playwright install chromium
    make test-browser
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from rigging.events import EventLog

pytest.importorskip("playwright.sync_api", reason="needs the [browser] extra")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from rigging.service import create_app

pytestmark = pytest.mark.browser


def _finished_run(tmp_path) -> tuple[EventLog, str]:
    log = EventLog(tmp_path / "runs.db")
    session_id = log.start_session(title="browser smoke")
    run_id = log.start_run(session_id=session_id, model="fake", spec_hash="abc")
    log.append(run_id, "input_received", {"content": "hello"})
    log.append(run_id, "model_response", {"content": "hi", "usage": {"input_tokens": 3}})
    log.append(run_id, "run_finished", {"status": "ok"})
    log.finish_run(run_id, "ok")
    return log, run_id


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """Run the ASGI app on a real loopback socket for the browser to hit."""

    def __init__(self, db: str, port: int) -> None:
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(create_app(db), host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return f"http://127.0.0.1:{self._server.servers[0].sockets[0].getsockname()[1]}"
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def test_the_page_renders_the_run_list(tmp_path):
    log, run_id = _finished_run(tmp_path)
    with _Server(str(log.path), _free_port()) as base_url, sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except PlaywrightError as exc:  # no Chromium downloaded
            pytest.skip(f"chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        console: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: console.append(msg.text))
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector(".run", timeout=10_000)
            assert run_id in page.inner_text("#runs")
            assert "browser smoke" in page.inner_text("#runs")
            assert errors == [], f"the page raised: {errors}"
            violations = [m for m in console if "Content Security Policy" in m]
            assert violations == [], f"the CSP blocked the page's own code: {violations}"
        finally:
            browser.close()
