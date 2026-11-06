"""Web view of the event log: read-only by default, chat when a manifest is given.

Everything the page shows is a query over `sessions`, `runs` and `run_events`.
Without `--manifest` there is no write endpoint at all (ADR-0018). With it, the
page can start a run and answer an approval, and the loop still runs the agent
(ADR-0023). Needs `rigging[service]`; `harness serve` prints the install hint
without it.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from rigging.errors import RiggingError
from rigging.events import project_approvals, project_escalations
from rigging.service.chat import ChatRunner
from rigging.service.openai_api import (
    PUBLIC_MESSAGES,
    ChatCompletionRequest,
    ModelNotFound,
    OpenAIBridge,
    RequestError,
    TurnFailed,
)
from rigging.service.ratelimit import RateLimiter

#: How often the SSE generator re-reads the log while a run is in flight.
POLL_INTERVAL_S = 0.3


def _connect(db: str | Path) -> sqlite3.Connection:
    """Open the log read-only. `mode=ro` makes the read-only claim enforceable."""
    path = Path(db)
    if not path.exists():
        raise FileNotFoundError(f"no event log at {path} — run an agent first")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args)]


def _one(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


_RUN_SELECT = (
    "SELECT r.*, s.title FROM runs r LEFT JOIN sessions s USING(session_id)"
)


def _run_row(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    return _one(conn, f"{_RUN_SELECT} WHERE r.run_id=?", (run_id,))


def _event_rows(conn: sqlite3.Connection, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT seq, ts, type, payload, span_id FROM run_events"
        " WHERE run_id=? AND seq>? ORDER BY seq",
        (run_id, after_seq),
    )
    for row in rows:
        row["payload"] = json.loads(row["payload"])  # already redacted on write
    return rows


def runs(db: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    with closing(_connect(db)) as conn:
        return _rows(conn, f"{_RUN_SELECT} ORDER BY r.started_at DESC LIMIT ?", (limit,))


def session_runs(db: str | Path, session_id: str) -> list[dict[str, Any]]:
    """A session's root runs, oldest first: its conversation, not its children."""
    with closing(_connect(db)) as conn:
        return _rows(
            conn,
            f"{_RUN_SELECT} WHERE r.session_id=? AND r.parent_run_id IS NULL"
            " ORDER BY r.started_at, r.run_id",
            (session_id,),
        )


def escalations(db: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """Handoffs waiting for a human, newest first — the same projection as the CLI."""
    with closing(_connect(db)) as conn:
        return project_escalations(conn, limit)


def approvals(db: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """Side-effecting calls waiting for a human (ADR-0029), same as the CLI."""
    with closing(_connect(db)) as conn:
        return project_approvals(conn, limit)


def _session_running(db: str | Path, session_id: str) -> bool:
    """True while a turn of this session is still in flight (ADR-0023)."""
    with closing(_connect(db)) as conn:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE session_id=? AND status='running' LIMIT 1",
            (session_id,),
        ).fetchone()
    return row is not None


def run(db: str | Path, run_id: str) -> dict[str, Any] | None:
    with closing(_connect(db)) as conn:
        return _run_row(conn, run_id)


def events(db: str | Path, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
    with closing(_connect(db)) as conn:
        return _event_rows(conn, run_id, after_seq=after_seq)


def _sse(data: dict[str, Any]) -> str:
    """One default-named SSE frame, so any client (`curl`, `EventSource`) sees it."""
    return f"data: {json.dumps(data, default=str)}\n\n"


def _tail(db: str | Path, run_id: str, idle_seconds: float, max_seconds: float) -> Iterator[str]:
    """Replay the run's events, then follow it until it finishes or goes quiet.

    Each poll opens and closes its own read-only connection. Holding one across
    `yield` is unsafe: when a client disconnects, Starlette finalises the
    generator on another thread, and SQLite refuses a close from a foreign
    thread.
    """
    yield "retry: 2000\n\n"
    seq = 0
    began = time.monotonic()
    quiet_since = began
    while True:
        batch = events(db, run_id, after_seq=seq)
        for ev in batch:
            seq = ev["seq"]
            yield _sse(ev)
        if batch:
            quiet_since = time.monotonic()

        row = run(db, run_id)
        if row is None:
            yield "event: end\ndata: {\"reason\": \"run disappeared\"}\n\n"
            return
        if row["status"] != "running" and not batch:
            yield (f"event: end\ndata: {{\"reason\": \"run finished\", "
                   f"\"status\": \"{row['status']}\"}}\n\n")
            return

        now = time.monotonic()
        if now - began > max_seconds:
            yield "event: end\ndata: {\"reason\": \"max_seconds\"}\n\n"
            return
        if now - quiet_since > idle_seconds:
            # Still running: end the stream and let the page reopen it on demand.
            yield "event: end\ndata: {\"reason\": \"idle\"}\n\n"
            return
        time.sleep(POLL_INTERVAL_S)


def _is_loopback(host: str) -> bool:
    """True for `localhost` and any loopback address, including the IPv6 forms.

    An empty host is *not* loopback: uvicorn binds it to every interface.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(
    host: str, tokens: Sequence[str], *, tls: bool = False, allow_plaintext: bool = False
) -> None:
    """Refuse an unsafe exposure (ADR-0021).

    A loopback bind is always fine. Anything else needs a token *and* either TLS
    on the socket or an explicit opt-in, because a bearer token over plain HTTP
    crosses the network in clear text.
    """
    if _is_loopback(host):
        return
    if not tokens:
        raise ValueError(
            f"refusing to bind {host!r} without RIGGING_SERVICE_TOKEN: the web view "
            "serves full run transcripts. Set a token, or bind 127.0.0.1."
        )
    if tls or allow_plaintext:
        return
    raise ValueError(
        f"refusing to bind {host!r} with a bearer token over plain HTTP: the token "
        "would cross the network in clear text. Terminate TLS in a reverse proxy "
        "and bind 127.0.0.1 (docs/threat-model.md), pass --ssl-certfile and "
        "--ssl-keyfile, or set RIGGING_SERVICE_ALLOW_PLAINTEXT=1 when the network "
        "already protects the link."
    )


def check_trusted_proxies(value: str) -> None:
    """Refuse a trust list that trusts everyone.

    `X-Forwarded-For` is only meaningful because a proxy in the trust list
    overwrites or appends it. A `*` entry (or an all-addresses network) means any
    client can pick its own rate-limit bucket and frame another client, so it is
    refused rather than accepted with a warning.
    """
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        if entry == "*":
            raise ValueError(
                "refusing a trusted-proxy list of '*': every client could choose "
                "its own rate-limit bucket. Name the proxy address instead."
            )
        if "/" in entry:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if network.prefixlen == 0:
                raise ValueError(
                    f"refusing trusted proxy {entry!r}: it trusts every address, so "
                    "X-Forwarded-For is spoofable. Name the proxy address instead."
                )


def _auth_dependency(tokens: tuple[str, ...]) -> Any:
    def require_bearer(request: Request) -> None:
        if not tokens:
            return
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        valid = scheme.lower() == "bearer" and any(
            hmac.compare_digest(value, token) for token in tokens
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_bearer


def _rate_limit_dependency(limiter: RateLimiter) -> Any:
    """Count every `/api/*` request per client IP, before the token is checked.

    Before auth on purpose: a script guessing tokens should hit 429 on its own
    budget, not get unlimited free comparisons against `hmac.compare_digest`.

    The key is `request.client.host`. Whether that is the socket peer or an
    `X-Forwarded-For` value is decided by the ASGI server *before* the app runs:
    `harness serve` passes the trusted-proxy list to uvicorn, which rewrites the
    client only when the immediate peer is in it. The app cannot re-verify that,
    and must not parse the header itself.
    """

    def enforce(request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        allowed, retry_after = limiter.allow(key)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="too many requests",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    return enforce


def _openai_error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    """An error a chat UI can render: OpenAI's envelope, not FastAPI's `detail`."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": status}},
    )


def _csp(nonce: str) -> str:
    """Only the page's own inline script and style may run; no framing, no forms."""
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


class ChatRequest(BaseModel):
    """One turn from the page. `session_id` continues an existing conversation."""

    prompt: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None


class ApprovalRequest(BaseModel):
    """The page's answer to a consequential tool."""

    approve: bool
    note: str = Field(default="", max_length=500)


class ApprovalDecision(BaseModel):
    """An answer to a *deferred* approval, from a client that was not there (ADR-0029)."""

    approve: bool
    approver: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)


def create_app(
    db: str | Path,
    *,
    idle_seconds: float = 20.0,
    max_seconds: float = 600.0,
    tokens: Sequence[str] | str | None = None,
    rate_limit_per_minute: int = 240,
    runner: ChatRunner | None = None,
    bridge: OpenAIBridge | None = None,
) -> FastAPI:
    """Build the app around one SQLite event log.

    `tokens` guards every `/api/*` and `/v1/*` route; the HTML shell stays public
    so the page can ask for the token (ADR-0021). Empty tokens means no auth —
    only valid for a loopback bind, which `harness serve` enforces.
    `rate_limit_per_minute` caps requests per client IP per minute; `0` disables
    it.

    `runner` turns on the chat write path (ADR-0023). It is refused without a
    token: a page on any origin can POST to a loopback port, and the token is the
    one thing that page cannot read.

    `bridge` turns on the OpenAI-compatible `/v1` surface for a chat UI (ADR-0028).
    It is refused without a token for the same reason, with less ambiguity: a
    chat-completions endpoint is exactly the shape every one of those tools
    already knows how to call.
    """
    if not Path(db).exists():
        raise FileNotFoundError(f"no event log at {db} — run an agent first")
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.split(",") if t.strip()]
    token_tuple = tuple(tokens or ())
    if runner is not None and not token_tuple:
        raise ValueError(
            "refusing to enable the chat write path without RIGGING_SERVICE_TOKEN: "
            "any page the browser visits can POST to a loopback port. Set a token "
            "or start `harness serve` without --manifest (read-only)."
        )
    if bridge is not None and not token_tuple:
        raise ValueError(
            "refusing to serve /v1/chat/completions without RIGGING_SERVICE_TOKEN: "
            "the OpenAI surface is a write path, and any client that can reach the "
            "port could run the agent. Set a token or drop --openai-api."
        )
    limiter = RateLimiter(rate_limit_per_minute)
    guard = [Depends(_rate_limit_dependency(limiter)), Depends(_auth_dependency(token_tuple))]

    app = FastAPI(title="rigging", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = _csp(nonce)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        nonce = getattr(request.state, "csp_nonce", "")
        return INDEX_HTML.replace("__CSP_NONCE__", nonce)

    @app.get("/health")
    def health() -> Response:
        """Readiness for whatever is watching the port: an IDE preview, docker, a proxy.

        Public and tokenless like `GET /` — it carries no run data, only whether
        this process can still read the log it serves. 503 means the event log
        moved or vanished under a running server. Unrate-limited on purpose: a
        probe that gets throttled reads as an outage.
        """
        try:
            with closing(_connect(db)) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:  # any failure here is "not ready"
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/runs", dependencies=guard)
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return {"runs": runs(db, max(1, min(limit, 100)))}

    @app.get("/api/escalations", dependencies=guard)
    def list_escalations(limit: int = 50) -> dict[str, Any]:
        return {"escalations": escalations(db, limit)}

    @app.get("/api/approvals", dependencies=guard)
    def list_approvals(limit: int = 50) -> dict[str, Any]:
        return {"approvals": approvals(db, max(1, min(limit, 100)))}

    @app.get("/api/config", dependencies=guard)
    def config() -> dict[str, Any]:
        """What the page may do: chat needs a manifest and a token (ADR-0023)."""
        return {
            "chat": runner is not None,
            "manifest": runner.spec.name if runner is not None else None,
            "model": runner.spec.model if runner is not None else None,
            "approvals": runner.approvals if runner is not None else None,
        }

    if runner is not None:

        @app.post("/api/chat", dependencies=guard, status_code=202)
        def chat_turn(body: ChatRequest) -> dict[str, Any]:
            if body.session_id and _session_running(db, body.session_id):
                raise HTTPException(
                    status_code=409,
                    detail="a turn is still running for this session — wait for it",
                )
            session_id, run_id = runner.start(body.prompt, body.session_id)
            return {"session_id": session_id, "run_id": run_id}

        @app.post("/api/runs/{run_id}/approval", dependencies=guard)
        def answer_approval(run_id: str, body: ApprovalRequest) -> dict[str, Any]:
            if not runner.resolve(run_id, body.approve, body.note):
                raise HTTPException(
                    status_code=409, detail="no approval is waiting for this run"
                )
            return {"run_id": run_id, "approved": body.approve}

        @app.post("/api/approvals/{approval_id}", dependencies=guard)
        def answer_deferred(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
            """Answer a parked run and let it continue (ADR-0029).

            The token is the authority here, and `approver` is a label the caller
            declares: this harness has no accounts and does not claim to.
            """
            try:
                result = runner.answer_deferred(
                    approval_id, approved=body.approve,
                    approver=body.approver.strip() or "service",
                    note=body.note,
                )
            except LookupError:
                raise HTTPException(
                    status_code=409, detail="no approval is waiting for this id"
                ) from None
            except RiggingError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            return {"run_id": result.run_id, "status": result.status,
                    "approved": body.approve, "output": result.output}

    if bridge is not None:

        @app.get("/v1/models", dependencies=guard)
        def openai_models() -> dict[str, Any]:
            """What a UI's model picker lists: one entry, the agent's name.

            Not the manifest's model id: a manifest can change models without the
            UI being reconfigured, and the UI's id should mean "this agent".
            """
            return {"object": "list", "data": [{
                "id": bridge.model_id,
                "object": "model",
                "created": 0,
                "owned_by": "rigging",
            }]}

        @app.post("/v1/chat/completions", dependencies=guard)
        def openai_chat(request: Request, body: ChatCompletionRequest) -> Response:
            """The OpenAI write path (ADR-0028): one agent, one turn, no approvals."""
            try:
                turn = bridge.prepare(body, request.headers.get("x-conversation-id"))
            except ModelNotFound:
                return _openai_error(404, PUBLIC_MESSAGES["model_not_found"],
                                     "model_not_found")
            except RequestError as exc:
                # The response body is a constant looked up by code. Returning
                # `str(exc)` would let an internal detail reach the client
                # (CodeQL: information exposure through an exception).
                return _openai_error(
                    400, PUBLIC_MESSAGES.get(exc.code, PUBLIC_MESSAGES["invalid_request"])
                )
            except RiggingError:
                return _openai_error(400, PUBLIC_MESSAGES["invalid_request"])
            if _session_running(db, turn.session_id):
                # The same rule as the page (ADR-0023): two turns of one
                # conversation cannot interleave into one history.
                return _openai_error(409, "a turn is still running for this conversation",
                                     "turn_in_progress")
            if body.stream:
                return StreamingResponse(
                    bridge.stream(turn, body),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            try:
                return JSONResponse(bridge.complete(turn, body))
            except TurnFailed:
                return _openai_error(502, PUBLIC_MESSAGES["run_failed"], "run_failed")

    @app.get("/api/runs/{run_id}", dependencies=guard)
    def get_run(run_id: str) -> dict[str, Any]:
        row = run(db, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such run")
        return {"run": row, "events": events(db, run_id)}

    @app.get("/api/sessions/{session_id}", dependencies=guard)
    def get_session(session_id: str) -> dict[str, Any]:
        """Every turn of a session, oldest first — the conversation, not one run."""
        rows = session_runs(db, session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="no such session")
        return {"session_id": session_id,
                "turns": [{"run": row, "events": events(db, row["run_id"])} for row in rows]}

    @app.get("/api/runs/{run_id}/stream", dependencies=guard)
    def stream(
        run_id: str, idle: float | None = None, timeout: float | None = None
    ) -> StreamingResponse:
        if run(db, run_id) is None:
            raise HTTPException(status_code=404, detail="no such run")
        return StreamingResponse(
            _tail(
                db,
                run_id,
                idle if idle is not None else idle_seconds,
                timeout if timeout is not None else max_seconds,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rigging — runs</title>
<style nonce="__CSP_NONCE__">
  :root {
    --bg: #101418; --panel: #171c22; --line: #232b34; --ink: #d7dee7;
    --dim: #7b8794; --accent: #7dd3a7; --warn: #f0b46b; --bad: #ef7f7f;
    --user: #1d2833; --call: #16202a;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  header { padding: 14px 18px; border-bottom: 1px solid var(--line);
           display: flex; gap: 14px; align-items: baseline; }
  header b { letter-spacing: .04em; }
  header span { color: var(--dim); font-size: 12px; }
  main { display: grid; grid-template-columns: 300px 1fr; height: calc(100vh - 53px); }
  aside { border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
  #tabs { display: flex; gap: 6px; padding: 8px 8px 0; }
  .tab { flex: 1; padding: 5px 8px; background: transparent; color: var(--dim);
         border: 1px solid var(--line); border-radius: 6px; font: inherit;
         font-size: 12px; cursor: pointer; }
  .tab:hover { border-color: var(--dim); }
  .tab.active { color: var(--ink); background: var(--panel); }
  .tab .count { color: var(--warn); }
  #new { margin: 8px; padding: 7px 10px; background: var(--panel); color: var(--ink);
         border: 1px solid var(--line); border-radius: 6px; cursor: pointer; font: inherit; }
  #new:hover { border-color: var(--dim); }
  #runs { overflow: auto; padding: 0 8px 8px; flex: 1; }
  #escalations { overflow: auto; padding: 0 8px 8px; flex: 1; }
  #runs[hidden], #escalations[hidden] { display: none; }
  .run { padding: 8px 10px; border-radius: 6px; cursor: pointer; }
  .run:hover { background: var(--panel); }
  .run[aria-current="true"] { background: var(--panel); outline: 1px solid var(--line); }
  .run small { color: var(--dim); display: block; font-variant-numeric: tabular-nums; }
  .esc { padding: 8px 10px; border-radius: 6px; cursor: pointer; }
  .esc:hover { background: var(--panel); }
  .esc[aria-current="true"] { background: var(--panel); outline: 1px solid var(--line); }
  .esc .reason { color: var(--warn); font-weight: 600; font-size: 12px; }
  .esc small { color: var(--dim); display: block; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .pill { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); }
  .ok { color: var(--accent); } .running { color: var(--warn); } .bad { color: var(--bad); }
  section { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #meta { padding: 10px 16px; border-bottom: 1px solid var(--line); color: var(--dim);
          font-size: 12px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  #meta label { display: flex; gap: 5px; align-items: center; cursor: pointer; }
  #view { flex: 1; overflow: auto; padding: 16px; }
  #composer { display: flex; gap: 8px; padding: 10px 16px; border-top: 1px solid var(--line); }
  #composer[hidden] { display: none; }
  #prompt { flex: 1; resize: none; padding: 9px 11px; border-radius: 8px; font: inherit;
            background: var(--panel); color: var(--ink); border: 1px solid var(--line); }
  #prompt:focus { outline: 1px solid var(--dim); }
  #send { padding: 0 16px; border-radius: 8px; border: 1px solid var(--line);
          background: var(--accent); color: #0c1410; font-weight: 600; cursor: pointer; }
  #send:disabled { background: var(--panel); color: var(--dim); cursor: default; }
  .empty { color: var(--dim); padding: 24px 4px; }
  .msg { max-width: 78ch; margin: 0 0 12px; padding: 9px 13px; border-radius: 10px;
         white-space: pre-wrap; word-break: break-word; }
  .user { background: var(--user); border: 1px solid var(--line); margin-left: auto; }
  .agent { background: transparent; border: 1px solid var(--line); }
  .msg.md { white-space: normal; }
  .md p { margin: 0 0 8px; }
  .md p:last-child { margin-bottom: 0; }
  .md h3, .md h4, .md h5, .md h6 { margin: 12px 0 6px; font-size: 1.05em; }
  .md ul, .md ol { margin: 0 0 8px; padding-left: 22px; }
  .md li { margin: 2px 0; }
  .md code { background: #0c1116; border: 1px solid var(--line); border-radius: 4px;
             padding: 1px 4px; font: 12px ui-monospace, monospace; }
  .md pre { margin: 0 0 8px; padding: 10px 12px; overflow: auto; background: #0c1116;
            border: 1px solid var(--line); border-radius: 6px; }
  .md pre code { padding: 0; border: 0; background: transparent; }
  .md blockquote { margin: 0 0 8px; padding-left: 10px; border-left: 2px solid var(--line);
                   color: var(--dim); }
  .md a { color: #8ab4f8; }
  .md hr { border: 0; border-top: 1px solid var(--line); margin: 10px 0; }
  .table-wrap { overflow-x: auto; margin: 8px 0; }
  .md table { border-collapse: collapse; font-size: 13px; }
  .md th, .md td { border: 1px solid var(--line); padding: 4px 9px; text-align: left;
                   vertical-align: top; }
  .md th { background: var(--panel); font-weight: 600; }
  .md .right { text-align: right; }
  .md .center { text-align: center; }
  .card { max-width: 88ch; margin: 0 0 10px; border: 1px solid var(--line);
          border-radius: 8px; background: var(--call); overflow: hidden;
          cursor: pointer; }
  .card > .head { display: flex; gap: 8px; align-items: baseline; padding: 7px 11px;
                  border-bottom: 1px solid var(--line); font: 12px ui-monospace, monospace; }
  .card > .head .name { color: #8ab4f8; }
  .card > .head .fold { margin-left: auto; padding: 0 4px; border: 0; background: transparent;
                        color: var(--dim); font: inherit; cursor: pointer; }
  .card > .head .fold:hover { color: var(--ink); }
  .card.folded > .head { border-bottom: 0; }
  .card.folded > .body { display: none; }
  .card > .body { padding: 8px 11px; font: 12px/1.55 ui-monospace, monospace;
                  white-space: pre-wrap; word-break: break-word; color: var(--dim); }
  .card > .body.out { color: var(--ink); border-top: 1px dashed var(--line); }
  .card.denied { border-color: #5a2b2b; }
  .card.denied .name { color: var(--bad); }
  .note { font-size: 12px; color: var(--dim); margin: -4px 0 10px; padding-left: 2px;
          font: 12px ui-monospace, monospace; }
  .note .hook { color: #8ab4f8; }
  .approve { display: flex; gap: 8px; padding: 9px 11px; border-top: 1px solid var(--line); }
  .approve button { padding: 5px 14px; border-radius: 6px; border: 1px solid var(--line);
                    background: var(--panel); color: var(--ink); font: inherit; cursor: pointer; }
  .approve button.yes { border-color: var(--accent); color: var(--accent); }
  .approve button.no { border-color: var(--bad); color: var(--bad); }
  .approve button:disabled { color: var(--dim); border-color: var(--line); cursor: default; }
  .foot { margin: 14px 0 4px; padding-top: 10px; border-top: 1px solid var(--line);
          font-size: 12px; color: var(--dim); display: flex; gap: 14px; flex-wrap: wrap; }
  .raw { display: grid; grid-template-columns: 90px 170px 1fr; gap: 10px;
         border-bottom: 1px solid #1b2229; padding: 3px 0; font: 12px/1.6 ui-monospace, monospace; }
  .raw time { color: #5c6773; }
  .raw .type { color: #8ab4f8; }
  .raw .body { white-space: pre-wrap; word-break: break-word; }
</style>
<header>
  <b>rigging</b>
  <span id="mode">read-only view of the event log</span>
  <span id="whoami"></span>
</header>
<main>
  <aside>
    <div id="tabs">
      <button id="tab-runs" class="tab active" type="button">runs</button>
      <button id="tab-esc" class="tab" type="button">escalations <span id="esc-count" class="count"></span></button>
    </div>
    <button id="new" type="button">+ new session</button>
    <div id="runs"><p class="empty">loading runs…</p></div>
    <div id="escalations" hidden><p class="empty">loading escalations…</p></div>
  </aside>
  <section>
    <div id="meta"><span>pick a run</span></div>
    <div id="view"><p class="empty">No run selected.</p></div>
    <form id="composer" hidden>
      <textarea id="prompt" rows="1"
        placeholder="Ask the agent — e.g. refund ORD-1002, 10 units, 112.50 USD"></textarea>
      <button id="send" type="submit">send</button>
    </form>
  </section>
</main>
<script nonce="__CSP_NONCE__">
const runsEl = document.getElementById('runs');
const escEl = document.getElementById('escalations');
const escCount = document.getElementById('esc-count');
const tabRuns = document.getElementById('tab-runs');
const tabEsc = document.getElementById('tab-esc');
const viewEl = document.getElementById('view');
const metaEl = document.getElementById('meta');
const modeEl = document.getElementById('mode');
const whoEl = document.getElementById('whoami');
const composer = document.getElementById('composer');
const promptEl = document.getElementById('prompt');
const sendEl = document.getElementById('send');

let token = sessionStorage.getItem('rigging.token') || '';
let streamController = null;
let config = { chat: false, manifest: null, model: null };
let currentRun = null, currentSession = null, currentStatus = null;
let events = [], showRaw = false, busy = false;
// Folded cards survive a re-render, which happens on every streamed event.
const folded = new Set();
let panel = 'runs';

const authHeaders = () => (token ? { Authorization: `Bearer ${token}` } : {});

// The shell is public, the data is not (ADR-0021). On the first 401, ask for the
// token and keep it in sessionStorage — never in the URL, never in a log.
async function api(path, opts) {
  const options = Object.assign({}, opts || {});
  options.headers = Object.assign({}, options.headers || {}, authHeaders());
  let res = await fetch(path, options);
  if (res.status === 401) {
    const entered = prompt('Bearer token for this rigging service (RIGGING_SERVICE_TOKEN):');
    if (!entered) throw new Error('unauthorized');
    token = entered.trim();
    sessionStorage.setItem('rigging.token', token);
    options.headers = Object.assign({}, options.headers || {}, authHeaders());
    res = await fetch(path, options);
  }
  return res;
}

async function postJson(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString();

function pill(status) {
  const cls = status === 'ok' ? 'ok' : status === 'running' ? 'running' : 'bad';
  return `<span class="pill ${cls}">${esc(status)}</span>`;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ------------------------------------------------------------------ markdown
// Agent output is untrusted. Escape it first, then add only the tags below; no
// HTML from the response reaches the DOM, and alignment is a class because the
// CSP nonce allows the page's <style> but not inline style attributes.
const INLINE_SLOT = /\u0000(\d+)\u0000/g;

function inlineMd(text) {
  const code = [];
  let out = esc(text).replace(/`([^`\n]+)`/g, (_, value) => {
    code.push(value);
    return `\u0000${code.length - 1}\u0000`;
  });
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  out = out.replace(/~~([^~\n]+)~~/g, '<s>$1</s>');
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return out.replace(INLINE_SLOT, (_, index) => `<code>${code[Number(index)]}</code>`);
}

const splitRow = (line) => {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|')) row = row.slice(0, -1);
  return row.split('|').map((cell) => cell.trim());
};

const isTableSep = (line) => {
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
};

const alignOf = (sep) => (sep.startsWith(':') && sep.endsWith(':') ? 'center'
  : sep.endsWith(':') ? 'right' : sep.startsWith(':') ? 'left' : '');

function markdownTable(header, aligns, rows) {
  const table = el('table');
  const thead = el('thead');
  const head = el('tr');
  header.forEach((cell, index) => {
    const th = el('th', aligns[index] || '');
    th.innerHTML = inlineMd(cell);
    head.appendChild(th);
  });
  thead.appendChild(head);
  table.appendChild(thead);
  const body = el('tbody');
  rows.forEach((row) => {
    const tr = el('tr');
    header.forEach((_cell, index) => {
      const td = el('td', aligns[index] || '');
      td.innerHTML = inlineMd(row[index] || '');
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  const wrap = el('div', 'table-wrap');
  wrap.appendChild(table);
  return wrap;
}

function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  let para = [];

  const flush = () => {
    if (!para.length) return;
    const node = el('p');
    node.innerHTML = inlineMd(para.join(' '));
    frag.appendChild(node);
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { flush(); i += 1; continue; }

    const fence = /^```(\w*)\s*$/.exec(line);
    if (fence) {
      flush();
      const body = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      if (i < lines.length) i += 1;
      const pre = el('pre');
      pre.appendChild(el('code', fence[1] ? `language-${fence[1]}` : '', body.join('\n')));
      frag.appendChild(pre);
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flush();
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map(alignOf);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      frag.appendChild(markdownTable(header, aligns, rows));
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const node = el(`h${Math.min(heading[1].length + 2, 6)}`);
      node.innerHTML = inlineMd(heading[2]);
      frag.appendChild(node);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      frag.appendChild(el('hr'));
      i += 1;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flush();
      const quoted = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      const node = el('blockquote');
      node.innerHTML = inlineMd(quoted.join(' '));
      frag.appendChild(node);
      continue;
    }

    const item = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (item) {
      flush();
      const ordered = /\d/.test(item[1]);
      const list = el(ordered ? 'ol' : 'ul');
      while (i < lines.length) {
        const next = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!next || /\d/.test(next[1]) !== ordered) break;
        const li = el('li');
        li.innerHTML = inlineMd(next[2]);
        list.appendChild(li);
        i += 1;
      }
      frag.appendChild(list);
      continue;
    }

    para.push(line);
    i += 1;
  }
  flush();
  return frag;
}

function setBusy(value) {
  busy = value;
  sendEl.disabled = value;
  sendEl.textContent = value ? 'running…' : 'send';
}

async function loadConfig() {
  try {
    const res = await api('/api/config');
    if (res.ok) config = await res.json();
  } catch { /* read-only view still works */ }
  modeEl.textContent = config.chat
    ? 'runs and chat · ADR-0023'
    : 'read-only view of the event log';
  if (config.chat) {
    composer.hidden = false;
    whoEl.textContent = `${config.manifest} · ${config.model}`;
  }
}

async function loadRuns() {
  let res;
  try { res = await api('/api/runs?limit=50'); }
  catch {
    runsEl.innerHTML = '<p class="empty">could not load runs — unauthorized, or the server is gone. Reload to try again.</p>';
    return;
  }
  if (!res.ok) {
    runsEl.innerHTML = '<p class="empty">unauthorized — reload the page and enter the token</p>';
    return;
  }
  const { runs } = await res.json();
  if (!runs.length) {
    runsEl.innerHTML = config.chat
      ? '<p class="empty">no runs yet — ask the agent below</p>'
      : '<p class="empty">no runs yet — run <code>make hello</code></p>';
    return;
  }
  runsEl.replaceChildren(...runs.map((r) => {
    const node = el('div', 'run');
    const parent = r.parent_run_id ? `<small>↳ child of ${esc(r.parent_run_id)}</small>` : '';
    node.innerHTML = `<div>${esc(r.model || 'model?')} ${pill(r.status)}</div>
      <small>${esc(r.title || 'untitled')} · ${esc(r.run_id)}</small>
      ${parent}
      <small>${new Date(r.started_at * 1000).toLocaleString()}</small>`;
    node.addEventListener('click', () => openRun(r.run_id, { append: false }));
    node.dataset.run = r.run_id;
    if (r.run_id === currentRun) node.setAttribute('aria-current', 'true');
    return node;
  }));
}

async function loadEscalations() {
  let res;
  try { res = await api('/api/escalations?limit=50'); }
  catch {
    escEl.innerHTML = '<p class="empty">could not load escalations — unauthorized, or the server is gone.</p>';
    return;
  }
  if (!res.ok) {
    escEl.innerHTML = '<p class="empty">unauthorized — reload the page and enter the token</p>';
    return;
  }
  const { escalations } = await res.json();
  escCount.textContent = escalations.length ? String(escalations.length) : '';
  if (!escalations.length) {
    escEl.innerHTML = '<p class="empty">no handoffs waiting</p>';
    return;
  }
  escEl.replaceChildren(...escalations.map((e) => {
    const node = el('div', 'esc');
    const facts = e.facts && Object.keys(e.facts).length
      ? `<small>facts: ${esc(Object.keys(e.facts).join(', '))}</small>` : '';
    node.innerHTML = `<div><span class="reason">${esc(e.reason || 'unknown')}</span></div>
      <small>${esc(e.session_title || 'untitled')} · ${esc(e.run_id)}</small>
      <small>${esc((e.summary || '').split('\n')[0])}</small>
      ${facts}
      <small>${new Date(e.ts * 1000).toLocaleString()}</small>`;
    node.dataset.run = e.run_id;
    node.addEventListener('click', () => openRun(e.run_id, { append: false }));
    if (e.run_id === currentRun) node.setAttribute('aria-current', 'true');
    return node;
  }));
}

function selectPanel(name) {
  panel = name;
  const onRuns = name === 'runs';
  tabRuns.classList.toggle('active', onRuns);
  tabEsc.classList.toggle('active', !onRuns);
  runsEl.hidden = !onRuns;
  escEl.hidden = onRuns;
  if (onRuns) loadRuns(); else loadEscalations();
}

function newSession() {
  if (streamController) streamController.abort();
  if (panel !== 'runs') selectPanel('runs');
  currentRun = null; currentSession = null; currentStatus = null;
  events = [];
  metaEl.replaceChildren(el('span', '', 'new session'));
  viewEl.replaceChildren(el('p', 'empty', 'Type a message below to start a run.'));
  promptEl.focus();
}

// ---------------------------------------------------------------- transcript

const lastIndexOf = (type) => {
  for (let i = events.length - 1; i >= 0; i -= 1) if (events[i].type === type) return i;
  return -1;
};

function userBubble(text) { return el('div', 'msg user', text); }
function agentBubble(text) {
  const node = el('div', 'msg agent md');
  node.appendChild(renderMarkdown(text));
  return node;
}

function wireFold(card, key) {
  const button = card.querySelector('.fold');
  button.type = 'button';
  const paint = () => {
    const isFolded = folded.has(key);
    card.classList.toggle('folded', isFolded);
    button.textContent = isFolded ? '▸' : '▾';
    button.title = isFolded ? 'unfold the JSON payload' : 'fold the JSON payload';
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-expanded', String(!isFolded));
  };
  const toggle = () => {
    if (folded.has(key)) folded.delete(key); else folded.add(key);
    paint();
  };
  button.addEventListener('click', (ev) => { ev.stopPropagation(); toggle(); });
  // Anywhere else on the card folds too — except a control (approve/deny, a
  // link) or the tail of a text selection, where the click means "copy".
  card.addEventListener('click', (ev) => {
    if (ev.target.closest('button, a, .approve')) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    toggle();
  });
  paint();
}

function callCard(call) {
  const card = el('div', 'card');
  const head = el('div', 'head');
  head.innerHTML = `<span class="name">${esc(call.name)}</span><span>${esc(call.id || '')}</span>`;
  head.appendChild(el('button', 'fold', '▾'));
  card.appendChild(head);
  const args = JSON.stringify(call.arguments || {}, null, 2);
  card.appendChild(el('div', 'body', args));
  card.dataset.call = call.id || call.name;
  wireFold(card, `call:${call.id || call.name}`);
  return card;
}

function attachResult(card, result) {
  const body = el('div', 'body out');
  const prefix = result.denied ? 'denied · ' : result.is_error ? 'error · ' : '';
  body.textContent = prefix + String(result.content ?? '');
  card.appendChild(body);
  if (result.denied || result.is_error) card.classList.add('denied');
}

function guardrailNote(ev) {
  const checks = ev.payload.checks;
  const parts = Array.isArray(checks)
    ? checks.map((c) => (typeof c === 'string' ? c
        : `${c.guardrail || c.name || '?'} ${c.verdict || ''}`.trim())).join(' · ')
    : JSON.stringify(checks || '');
  const node = el('div', 'note');
  node.innerHTML = `<span class="hook">guardrail · ${esc(ev.payload.hook || '')}</span> `
    + `<span>${esc(parts)}</span>`;
  return node;
}

function approvalCard(ev, index, answeredAt) {
  const card = el('div', 'card');
  const head = el('div', 'head');
  head.innerHTML = `<span class="name">${esc(ev.payload.tool)}</span>
    <span>side-effecting · needs approval</span>`;
  head.appendChild(el('button', 'fold', '▾'));
  card.appendChild(head);
  card.appendChild(el('div', 'body', JSON.stringify(ev.payload.arguments || {}, null, 2)));
  wireFold(card, `approval:${ev.payload.tool}:${ev.ts}`);
  const row = el('div', 'approve');
  const yes = el('button', 'yes', 'approve');
  const no = el('button', 'no', 'deny');
  yes.addEventListener('click', () => answerApproval(true));
  no.addEventListener('click', () => answerApproval(false));
  row.append(yes, no);
  // Once answered, the verdict line below carries the outcome; buttons that do
  // nothing are worse than no buttons.
  if (answeredAt <= index && config.chat && config.approvals === 'page') {
    card.appendChild(row);
  }
  return card;
}

async function answerApproval(approve) {
  if (!currentRun) return;
  setBusy(true);
  try {
    const res = await postJson(`/api/runs/${currentRun}/approval`,
      { approve, note: approve ? 'approved in the web view' : 'denied in the web view' });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      viewEl.appendChild(el('p', 'note', detail.detail || 'could not answer the approval'));
      setBusy(false);
    }
  } catch {
    viewEl.appendChild(el('p', 'note', 'could not reach the service'));
    setBusy(false);
  }
}

function verdictCard(ev) {
  const approved = ev.payload.approved;
  const node = el('div', 'note');
  node.innerHTML = `<span class="hook">${approved ? 'approved' : 'denied'} · ${esc(ev.payload.tool)}</span> `
    + `<span>${esc(ev.payload.note || '')}</span>`;
  return node;
}

function footCard() {
  // Counts belong to the run on screen, not to the earlier turns preloaded above.
  const mine = events.filter((e) => !e.run_id || e.run_id === currentRun);
  const finished = mine.filter((e) => e.type === 'run_finished').pop();
  if (!finished) return null;
  const steps = mine.filter((e) => e.type === 'model_response').length;
  let tokens = 0, out = 0;
  for (const ev of mine) {
    if (ev.type !== 'model_response') continue;
    const usage = ev.payload.usage || {};
    tokens += Number(usage.input_tokens || 0);
    out += Number(usage.output_tokens || 0);
  }
  const node = el('div', 'foot');
  node.innerHTML = `<span>${pill(finished.payload.status || currentStatus || 'ok')}</span>
    <span>${steps} model turns</span>
    <span>${tokens} in / ${out} out tokens</span>
    <span>${esc(currentRun || '')}</span>`;
  return node;
}

function rawRow(ev) {
  const row = el('div', 'raw');
  row.innerHTML = `<time>${clock(ev.ts)}</time>
    <span class="type">${esc(ev.type)}</span><span class="body"></span>`;
  const payload = ev.payload || {};
  const brief = Object.keys(payload).length ? JSON.stringify(payload) : '';
  const text = payload.content ?? payload.error ?? brief;
  row.querySelector('.body').textContent =
    String(text).length > 600 ? String(text).slice(0, 600) + '…' : String(text);
  return row;
}

function renderAll() {
  if (!events.length) {
    viewEl.replaceChildren(el('p', 'empty',
      currentRun ? 'waiting for the first event…' : 'No run selected.'));
    return;
  }
  if (showRaw) {
    viewEl.replaceChildren(...events.map(rawRow));
    viewEl.scrollTop = viewEl.scrollHeight;
    return;
  }
  const nodes = [];
  const calls = new Map();
  const answeredAt = lastIndexOf('approval_verdict');
  events.forEach((ev, index) => {
    const p = ev.payload || {};
    switch (ev.type) {
      case 'input_received':
        nodes.push(userBubble(p.content || ''));
        break;
      case 'model_response':
        if (p.content) nodes.push(agentBubble(p.content));
        break;
      case 'tool_call': {
        const card = callCard(p);
        calls.set(p.id || p.name, card);
        nodes.push(card);
        break;
      }
      case 'tool_result': {
        const card = calls.get(p.call_id);
        if (card) attachResult(card, p);
        else {
          const lone = el('div', 'card');
          lone.appendChild(el('div', 'body out', String(p.content ?? '')));
          nodes.push(lone);
        }
        break;
      }
      case 'guardrail_verdict':
        nodes.push(guardrailNote(ev));
        break;
      case 'approval_requested':
        nodes.push(approvalCard(ev, index, answeredAt));
        break;
      case 'approval_verdict':
        nodes.push(verdictCard(ev));
        break;
      case 'escalation': {
        const facts = p.facts && Object.keys(p.facts).length
          ? ` · facts ${JSON.stringify(p.facts)}` : '';
        nodes.push(el('div', 'note',
          `escalated · ${p.reason || ''} — ${p.summary || ''}${facts}`));
        break;
      }
      case 'error':
        nodes.push(el('div', 'note', `error · ${p.error || ''}`));
        break;
      case 'run_finished':
        break;
      default:
        break;
    }
  });
  const foot = footCard();
  if (foot) nodes.push(foot);
  viewEl.replaceChildren(...nodes);
  viewEl.scrollTop = viewEl.scrollHeight;
}

// --------------------------------------------------------------------- runs

function renderMeta(run) {
  metaEl.replaceChildren(...[
    el('span', '', run.run_id),
    run.parent_run_id ? el('span', '', `child of ${run.parent_run_id}`) : null,
    el('span', '', run.model || ''),
    el('span', '', run.session_id || ''),
    el('span', '', run.title || 'untitled session'),
  ].filter(Boolean));
  const toggle = el('label');
  const box = el('input');
  box.type = 'checkbox';
  box.checked = showRaw;
  box.addEventListener('change', () => { showRaw = box.checked; renderAll(); });
  toggle.append(box, el('span', '', 'raw events'));
  metaEl.appendChild(toggle);
}

// The session is the conversation: opening a turn shows the turns before it.
async function preloadSession(sessionId, exceptRunId, beforeTs) {
  try {
    const res = await api(`/api/sessions/${sessionId}`);
    if (!res.ok) return;
    const { turns } = await res.json();
    const earlier = [];
    for (const turn of turns) {
      if (turn.run.run_id === exceptRunId) continue;
      if (beforeTs !== undefined && turn.run.started_at >= beforeTs) continue;
      earlier.push(...turn.events.map((e) => Object.assign({}, e, { run_id: turn.run.run_id })));
    }
    events = earlier;
  } catch { /* no history is not an error */ }
}

async function openRun(runId, opts) {
  const append = Boolean(opts && opts.append);
  currentRun = runId;
  if (!append) events = [];
  if (streamController) streamController.abort();
  [...runsEl.children].forEach((node) =>
    node.dataset.run === runId ? node.setAttribute('aria-current', 'true')
                              : node.removeAttribute('aria-current'));
  [...escEl.children].forEach((node) =>
    node.dataset.run === runId ? node.setAttribute('aria-current', 'true')
                              : node.removeAttribute('aria-current'));
  renderAll();
  setBusy(true);

  let run = null;
  try {
    const res = await api(`/api/runs/${runId}`);
    if (res.ok) run = (await res.json()).run;
  } catch { /* the stream reports the failure */ }
  if (run) {
    currentSession = run.session_id;
    currentStatus = run.status;
    renderMeta(run);
    if (!append && run.session_id) {
      await preloadSession(run.session_id, runId, run.started_at);
      renderAll();
    }
  }

  streamRun(runId);
}

// SSE over fetch, not EventSource: a header is possible here, a query token is not.
async function streamRun(runId) {
  streamController = new AbortController();
  const signal = streamController.signal;
  let res;
  try { res = await api(`/api/runs/${runId}/stream`); } catch { setBusy(false); return; }
  if (!res.ok || !res.body) { setBusy(false); return; }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (!handleFrame(frame)) return;
      }
    }
  } catch (err) {
    if (signal.aborted) return;  // a new run was selected
    throw err;
  }
}

function handleFrame(frame) {
  let event = 'message';
  const data = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7).trim();
    else if (line.startsWith('data: ')) data.push(line.slice(6));
  }
  if (event === 'end') {
    setBusy(false);
    loadRuns();
    if (panel === 'escalations') loadEscalations();
    return false;
  }
  if (data.length) {
    const ev = JSON.parse(data.join('\n'));
    ev.run_id = currentRun;
    events.push(ev);
    if (ev.type === 'run_finished' || ev.type === 'error') setBusy(false);
    renderAll();
  }
  return true;
}

// ------------------------------------------------------------------ compose

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = promptEl.value.trim();
  if (!text || busy) return;
  promptEl.value = '';
  setBusy(true);
  try {
    const res = await postJson('/api/chat', { prompt: text, session_id: currentSession });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      viewEl.appendChild(el('p', 'note', detail.detail || `could not start a run (${res.status})`));
      setBusy(false);
      return;
    }
    const started = await res.json();
    currentSession = started.session_id;
    // Append, so the previous turns of this session stay on screen.
    openRun(started.run_id, { append: true });
  } catch {
    viewEl.appendChild(el('p', 'note', 'could not reach the service'));
    setBusy(false);
  }
});

promptEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

document.getElementById('new').addEventListener('click', newSession);
tabRuns.addEventListener('click', () => selectPanel('runs'));
tabEsc.addEventListener('click', () => selectPanel('escalations'));

loadConfig().then(() => { selectPanel('runs'); if (config.chat) promptEl.focus(); });
</script>
</html>
"""
