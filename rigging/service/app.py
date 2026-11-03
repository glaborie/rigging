"""Read-only FastAPI + SSE view of the event log (ADR-0018).

Everything here is a query over `sessions`, `runs` and `run_events`. There is no
write endpoint and no second store, so the page can never disagree with the CLI.
Needs `rigging[service]`; `harness serve` prints the install hint without it.
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
from fastapi.responses import HTMLResponse, Response, StreamingResponse

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


def runs(db: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    with closing(_connect(db)) as conn:
        return _rows(
            conn,
            "SELECT r.*, s.title FROM runs r LEFT JOIN sessions s USING(session_id)"
            " ORDER BY r.started_at DESC LIMIT ?",
            (limit,),
        )


def run(db: str | Path, run_id: str) -> dict[str, Any] | None:
    with closing(_connect(db)) as conn:
        found = _rows(
            conn,
            "SELECT r.*, s.title FROM runs r LEFT JOIN sessions s USING(session_id)"
            " WHERE r.run_id=?",
            (run_id,),
        )
    return found[0] if found else None


def events(db: str | Path, run_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
    with closing(_connect(db)) as conn:
        rows = _rows(
            conn,
            "SELECT seq, ts, type, payload, span_id FROM run_events"
            " WHERE run_id=? AND seq>? ORDER BY seq",
            (run_id, after_seq),
        )
    for row in rows:
        row["payload"] = json.loads(row["payload"])  # already redacted on write
    return rows


def _sse(data: dict[str, Any]) -> str:
    """One default-named SSE frame, so any client (`curl`, `EventSource`) sees it."""
    return f"data: {json.dumps(data, default=str)}\n\n"


def _tail(db: str | Path, run_id: str, idle_seconds: float, max_seconds: float) -> Iterator[str]:
    """Replay the run's events, then follow it until it finishes or goes quiet."""
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
            yield f"event: end\ndata: {{\"reason\": \"run finished\", \"status\": \"{row['status']}\"}}\n\n"
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


def create_app(
    db: str | Path,
    *,
    idle_seconds: float = 20.0,
    max_seconds: float = 600.0,
    tokens: Sequence[str] | str | None = None,
    rate_limit_per_minute: int = 240,
) -> FastAPI:
    """Build the app around one SQLite event log.

    `tokens` guards every `/api/*` route; the HTML shell stays public so the page
    can ask for the token (ADR-0021). Empty tokens means no auth — only valid for
    a loopback bind, which `harness serve` enforces. `rate_limit_per_minute`
    caps requests per client IP per minute; `0` disables it.
    """
    if not Path(db).exists():
        raise FileNotFoundError(f"no event log at {db} — run an agent first")
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.split(",") if t.strip()]
    token_tuple = tuple(tokens or ())
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

    @app.get("/api/runs", dependencies=guard)
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return {"runs": runs(db, max(1, min(limit, 100)))}

    @app.get("/api/runs/{run_id}", dependencies=guard)
    def get_run(run_id: str) -> dict[str, Any]:
        row = run(db, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such run")
        return {"run": row, "events": events(db, run_id)}

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
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  header { padding: 14px 18px; border-bottom: 1px solid var(--line);
           display: flex; gap: 14px; align-items: baseline; }
  header b { letter-spacing: .04em; }
  header span { color: var(--dim); font-size: 12px; }
  main { display: grid; grid-template-columns: 300px 1fr; height: calc(100vh - 53px); }
  #runs { border-right: 1px solid var(--line); overflow: auto; padding: 8px; }
  .run { padding: 8px 10px; border-radius: 6px; cursor: pointer; }
  .run:hover { background: var(--panel); }
  .run[aria-current="true"] { background: var(--panel); outline: 1px solid var(--line); }
  .run small { color: var(--dim); display: block; font-variant-numeric: tabular-nums; }
  .pill { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); }
  .ok { color: var(--accent); } .running { color: var(--warn); } .bad { color: var(--bad); }
  section { display: flex; flex-direction: column; min-width: 0; }
  #meta { padding: 10px 16px; border-bottom: 1px solid var(--line); color: var(--dim);
          font-size: 12px; display: flex; gap: 16px; flex-wrap: wrap; }
  #log { flex: 1; overflow: auto; padding: 12px 16px; font: 12px/1.6 ui-monospace, monospace; }
  .ev { display: grid; grid-template-columns: 130px 170px 1fr; gap: 10px;
        border-bottom: 1px solid #1b2229; padding: 3px 0; }
  .ev time { color: #5c6773; }
  .ev .type { color: #8ab4f8; }
  .ev .body { white-space: pre-wrap; word-break: break-word; color: var(--ink); }
  .empty { color: var(--dim); padding: 24px 16px; }
</style>
<header><b>rigging</b><span>read-only view of the event log · ADR-0018</span></header>
<main>
  <div id="runs"><p class="empty">loading runs…</p></div>
  <section>
    <div id="meta"><span>pick a run</span></div>
    <div id="log"><p class="empty">No run selected.</p></div>
  </section>
</main>
<script nonce="__CSP_NONCE__">
const runsEl = document.getElementById('runs');
const logEl = document.getElementById('log');
const metaEl = document.getElementById('meta');
let token = sessionStorage.getItem('rigging.token') || '';
let streamController = null, current = null;

const authHeaders = () => (token ? { Authorization: `Bearer ${token}` } : {});

// The shell is public, the data is not (ADR-0021). On the first 401, ask for the
// token and keep it in sessionStorage — never in the URL, never in a log.
async function api(path) {
  let res = await fetch(path, { headers: authHeaders() });
  if (res.status === 401) {
    const entered = prompt('Bearer token for this rigging service (RIGGING_SERVICE_TOKEN):');
    if (!entered) throw new Error('unauthorized');
    token = entered.trim();
    sessionStorage.setItem('rigging.token', token);
    res = await fetch(path, { headers: authHeaders() });
  }
  return res;
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString();

function pill(status) {
  const cls = status === 'ok' ? 'ok' : status === 'running' ? 'running' : 'bad';
  return `<span class="pill ${cls}">${esc(status)}</span>`;
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
    runsEl.innerHTML = '<p class="empty">no runs yet — run <code>make hello</code></p>';
    return;
  }
  runsEl.replaceChildren(...runs.map((r) => {
    const el = document.createElement('div');
    el.className = 'run';
    const parent = r.parent_run_id ? `<small>↳ child of ${esc(r.parent_run_id)}</small>` : '';
    el.innerHTML = `<div>${esc(r.model || 'model?')} ${pill(r.status)}</div>
      <small>${esc(r.title || 'untitled')} · ${esc(r.run_id)}</small>
      ${parent}
      <small>${new Date(r.started_at * 1000).toLocaleString()}</small>`;
    el.onclick = () => openRun(r.run_id);
    el.dataset.run = r.run_id;
    if (r.run_id === current) el.setAttribute('aria-current', 'true');
    return el;
  }));
}

function render(ev) {
  const row = document.createElement('div');
  row.className = 'ev';
  const payload = ev.payload || {};
  const brief = Object.keys(payload).length ? JSON.stringify(payload) : '';
  row.innerHTML = `<time>${clock(ev.ts)}</time>
    <span class="type">${esc(ev.type)}</span>
    <span class="body"></span>`;
  const body = row.querySelector('.body');
  const text = payload.content ?? payload.error ?? brief;
  body.textContent = text.length > 600 ? text.slice(0, 600) + '…' : text;
  return row;
}

function openRun(runId) {
  current = runId;
  [...runsEl.children].forEach((el) =>
    el.dataset.run === runId ? el.setAttribute('aria-current', 'true')
                             : el.removeAttribute('aria-current'));
  logEl.replaceChildren();
  metaEl.innerHTML = `<span>${esc(runId)}</span>`;
  if (streamController) streamController.abort();

  api(`/api/runs/${runId}`).then((r) => r.json()).then(({ run }) => {
    metaEl.innerHTML = `<span>${esc(run.run_id)}</span>
      ${run.parent_run_id ? `<span>child of ${esc(run.parent_run_id)}</span>` : ''}
      <span>${esc(run.model || '')}</span>
      <span>${pill(run.status)}</span>
      <span>${esc(run.title || 'untitled session')}</span>
      <span>started ${new Date(run.started_at * 1000).toLocaleString()}</span>`;
  });

  streamRun(runId);
}

// SSE over fetch, not EventSource: a header is possible here, a query token is not.
async function streamRun(runId) {
  streamController = new AbortController();
  const signal = streamController.signal;
  let res;
  try { res = await api(`/api/runs/${runId}/stream`); } catch { return; }
  if (!res.ok || !res.body) return;

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
  if (event === 'end') { loadRuns(); return false; }
  if (data.length) {
    logEl.append(render(JSON.parse(data.join('\n'))));
    logEl.scrollTop = logEl.scrollHeight;
  }
  return true;
}

loadRuns();
</script>
</html>
"""
