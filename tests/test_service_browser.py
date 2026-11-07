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


def _finished_run(tmp_path, content: str = "hi") -> tuple[EventLog, str]:
    log = EventLog(tmp_path / "runs.db")
    session_id = log.start_session(title="browser smoke")
    run_id = log.start_run(session_id=session_id, model="fake", spec_hash="abc")
    log.append(run_id, "input_received", {"content": "hello"})
    log.append(run_id, "model_response", {"content": content, "usage": {"input_tokens": 3}})
    log.append(run_id, "run_finished", {"status": "ok"})
    log.finish_run(run_id, "ok")
    return log, run_id


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """Run the ASGI app on a real loopback socket for the browser to hit."""

    def __init__(self, db: str, port: int, **kwargs: object) -> None:
        import uvicorn

        self._server = uvicorn.Server(
            uvicorn.Config(create_app(db, **kwargs), host="127.0.0.1", port=port,
                           log_level="error")
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


def test_the_page_renders_markdown_tables_and_escapes_html(tmp_path):
    """A model answer is markdown, but it is also untrusted input."""
    content = (
        "Here is the quote:\n\n"
        "| SKU | Qty | Unit |\n"
        "|-----|----:|-----:|\n"
        "| SFP-10G-LR | 10 | `122.55` |\n"
        "| **SW-24P-GBE** | 2 | 289.00 |\n\n"
        "**Subtotal:** 1803.50\n\n"
        "<script>window.__pwned = 1</script>\n"
    )
    log, run_id = _finished_run(tmp_path, content)
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
            page.click(f".run[data-run='{run_id}']")
            page.wait_for_selector(".md table", timeout=10_000)

            assert page.locator(".md table").count() == 1
            assert "SKU" in page.locator(".md th").first.inner_text()
            assert page.locator(".md th.right").count() == 2  # Qty and Unit
            assert "SFP-10G-LR" in page.inner_text(".md table")
            assert "SW-24P-GBE" in page.inner_text(".md table")
            assert page.locator(".md strong").count() >= 1
            assert page.locator(".md code").count() >= 1

            # The script tag was escaped, not executed or parsed as an element.
            assert page.locator(".md script").count() == 0
            assert page.evaluate("() => window.__pwned") is None
            assert "<script>" in page.inner_text(".md")

            assert errors == [], f"the page raised: {errors}"
            violations = [m for m in console if "Content Security Policy" in m]
            assert violations == [], f"the CSP blocked the page's own code: {violations}"
        finally:
            browser.close()


def test_tool_cards_fold_and_unfold(tmp_path):
    """The JSON payload folds, and the choice survives a re-render of the log."""
    log = EventLog(tmp_path / "runs.db")
    session_id = log.start_session(title="browser smoke")
    run_id = log.start_run(session_id=session_id, model="fake", spec_hash="abc")
    log.append(run_id, "input_received", {"content": "look up ORD-1002"})
    log.append(run_id, "tool_call", {"id": "c1", "name": "lookup_order",
                                     "arguments": {"order_id": "ORD-1002"}})
    log.append(run_id, "tool_result", {"call_id": "c1", "name": "lookup_order",
                                       "content": '{"status": "delivered"}', "is_error": False})
    log.append(run_id, "model_response", {"content": "delivered", "usage": {"input_tokens": 3}})
    log.append(run_id, "run_finished", {"status": "ok"})
    log.finish_run(run_id, "ok")

    with _Server(str(log.path), _free_port()) as base_url, sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except PlaywrightError as exc:  # no Chromium downloaded
            pytest.skip(f"chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.click(f".run[data-run='{run_id}']")
            card = page.locator(".card").first
            card.locator(".fold").wait_for(timeout=10_000)
            assert "ORD-1002" in card.inner_text()
            assert card.locator(".body").first.is_visible()

            # Anywhere on the card folds, not just the header button.
            card.locator(".body").first.click()
            assert "folded" in (card.get_attribute("class") or "")
            assert not card.locator(".body").first.is_visible()
            assert card.locator(".fold").get_attribute("aria-expanded") == "false"

            # A re-render (raw toggle and back) must not resurrect the payload.
            page.check("#meta input[type=checkbox]")
            page.uncheck("#meta input[type=checkbox]")
            card = page.locator(".card").first
            assert "folded" in (card.get_attribute("class") or "")

            card.locator(".head").click()
            assert card.locator(".body").first.is_visible()
            assert card.locator(".fold").get_attribute("aria-expanded") == "true"
            assert errors == [], f"the page raised: {errors}"
        finally:
            browser.close()


def test_the_page_lists_escalations_and_opens_them(tmp_path):
    """The web queue: the tab lists the handoff, clicking it opens the run."""
    log, run_id = _finished_run(tmp_path)
    log.append(run_id, "escalation", {
        "reason": "customer_requested",
        "summary": "Wants a human to place the basket order",
        "facts": {"basket_id": "b_1be7fa75"},
        "transcript_run_id": run_id,
    })
    with _Server(str(log.path), _free_port()) as base_url, sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except PlaywrightError as exc:  # no Chromium downloaded
            pytest.skip(f"chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            page.click("#tab-esc")
            page.wait_for_selector(".esc", timeout=10_000)
            listing = page.inner_text("#escalations")
            assert "customer_requested" in listing
            assert "browser smoke" in listing
            assert "facts: basket_id" in listing

            page.click(".esc")
            page.wait_for_selector(".note", timeout=10_000)
            note = page.inner_text(".note")
            assert "escalated · customer_requested" in note
            assert "b_1be7fa75" in note
            assert errors == [], f"the page raised: {errors}"
        finally:
            browser.close()


def test_the_page_can_run_a_turn_and_approve_it(tmp_path):
    """ADR-0023 end to end: type, run, approve, see the tool result.

    The route tests prove the bytes; only a browser proves the page wires the
    composer to the write path and the approval buttons to the loop.
    """
    import yaml

    from rigging.manifest import load_manifest
    from rigging.registry import Registry
    from rigging.service import ChatRunner
    from rigging.testing import FakeClient

    db = tmp_path / "chat.db"
    EventLog(db).close()

    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "name": "browser chat", "model": "fake", "system_prompt": "be terse",
        "limits": {"max_steps": 4}, "tools": ["refund"],
        "approval": {"policy": "cli", "side_effecting_tools": ["refund"]},
    }))
    reg = Registry()

    def refund(amount: int) -> str:
        """Refund money."""
        return f"refunded {amount}"

    refund.__rigging_tool__ = {"name": "refund", "description": "Refund money.",
                               "side_effecting": True}
    reg.add("tool", "refund", refund)

    client = FakeClient([
        {"role": "assistant", "content": "checking the order",
         "tool_calls": [{"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "the refund went through"},
    ])
    runner = ChatRunner(load_manifest(str(path)), str(db),
                        client_factory=lambda: client, registry=reg)
    token = "browser-token"

    with _Server(str(db), _free_port(), tokens=[token], runner=runner) as base_url, \
            sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except PlaywrightError as exc:  # no Chromium downloaded
            pytest.skip(f"chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            # The page keeps the token in sessionStorage; set it before the first
            # fetch so no dialog has to be answered.
            page.add_init_script(
                f"sessionStorage.setItem('rigging.token', {token!r});"
            )
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector("#composer:not([hidden])", timeout=10_000)

            page.fill("#prompt", "refund 10 for ORD-1")
            page.press("#prompt", "Enter")

            page.wait_for_selector(".approve button.yes", timeout=10_000)
            prompt_card = page.locator(".card", has=page.locator(".approve"))
            assert "side-effecting" in prompt_card.inner_text()
            page.click(".approve button.yes")

            page.wait_for_selector("text=refunded 10", timeout=10_000)
            page.wait_for_selector(".foot", timeout=10_000)
            text = page.inner_text("#view")
            assert "the refund went through" in text
            assert errors == [], f"the page raised: {errors}"
        finally:
            browser.close()


def test_a_parked_approval_shows_its_id_and_can_be_answered(tmp_path):
    """ADR-0029 in the browser: the parked card names the id, and answering it resumes.

    The first version of this card showed no id at all, so a reviewer in the page
    had no way to answer the run it had just parked. The id is the handle.
    """
    import yaml

    from rigging.manifest import load_manifest
    from rigging.registry import Registry
    from rigging.service import ChatRunner
    from rigging.testing import FakeClient

    db = tmp_path / "deferred.db"
    EventLog(db).close()

    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump({
        "name": "browser deferred", "model": "fake", "system_prompt": "be terse",
        "limits": {"max_steps": 4}, "tools": ["refund"],
        # `deferred`, not `cli`: the run parks instead of blocking the thread.
        "approval": {"policy": "deferred", "side_effecting_tools": ["refund"]},
    }))
    reg = Registry()

    def refund(amount: int) -> str:
        """Refund money."""
        return f"refunded {amount}"

    refund.__rigging_tool__ = {"name": "refund", "description": "Refund money.",
                               "side_effecting": True}
    reg.add("tool", "refund", refund)

    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "the refund went through"},
    ])
    runner = ChatRunner(load_manifest(str(path)), str(db),
                        client_factory=lambda: client, registry=reg)
    token = "browser-token"

    with _Server(str(db), _free_port(), tokens=[token], runner=runner) as base_url, \
            sync_playwright() as play:
        try:
            browser = play.chromium.launch()
        except PlaywrightError as exc:  # no Chromium downloaded
            pytest.skip(f"chromium is not installed: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.add_init_script(
                f"sessionStorage.setItem('rigging.token', {token!r});"
            )
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_selector("#composer:not([hidden])", timeout=10_000)

            page.fill("#prompt", "refund 10 for ORD-1")
            page.press("#prompt", "Enter")

            page.wait_for_selector(".approve button.yes", timeout=10_000)
            card = page.locator(".card", has=page.locator(".approve")).first
            text = card.inner_text()
            assert "ap_" in text, f"the parked card hides the id: {text!r}"
            assert "refund" in text

            page.click(".approve button.yes")

            # The resume ran inside the POST, so the page shows the finished turn.
            page.wait_for_selector("text=the refund went through", timeout=10_000)
            assert "resumed · ap_" in page.inner_text("#view")
            assert errors == [], f"the page raised: {errors}"
        finally:
            browser.close()
