"""Read-only FastAPI + SSE view of the event log (ADR-0018).

Everything here is a query over `sessions`, `runs` and `run_events`. There is no
write endpoint and no second store, so the page can never disagree with the CLI.
Needs `rigging[service]`; `harness serve` prints the install hint without it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

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
    """One default-named SSE frame: `EventSource.onmessage` sees every event."""
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
            # The run is still going; the browser's EventSource reconnects.
            yield "event: end\ndata: {\"reason\": \"idle\"}\n\n"
            return
        time.sleep(POLL_INTERVAL_S)


def create_app(db: str | Path, *, idle_seconds: float = 20.0,
               max_seconds: float = 600.0) -> FastAPI:
    """Build the app around one SQLite event log."""
    if not Path(db).exists():
        raise FileNotFoundError(f"no event log at {db} — run an agent first")
    app = FastAPI(title="rigging", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/runs")
    def list_runs(limit: int = 20) -> dict[str, Any]:
        return {"runs": runs(db, max(1, min(limit, 100)))}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        row = run(db, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such run")
        return {"run": row, "events": events(db, run_id)}

    @app.get("/api/runs/{run_id}/stream")
    def stream(run_id: str, idle: float | None = None,
               timeout: float | None = None) -> StreamingResponse:
        if run(db, run_id) is None:
            raise HTTPException(status_code=404, detail="no such run")
        return StreamingResponse(
            _tail(db, run_id, idle if idle is not None else idle_seconds,
                  timeout if timeout is not None else max_seconds),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


INDEX_HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rigging — runs</title>
<style>
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
<script>
const runsEl = document.getElementById('runs');
const logEl = document.getElementById('log');
const metaEl = document.getElementById('meta');
let source = null, current = null;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString();

function pill(status) {
  const cls = status === 'ok' ? 'ok' : status === 'running' ? 'running' : 'bad';
  return `<span class="pill ${cls}">${esc(status)}</span>`;
}

async function loadRuns() {
  const res = await fetch('/api/runs?limit=50');
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
  if (source) source.close();

  fetch(`/api/runs/${runId}`).then((r) => r.json()).then(({ run }) => {
    metaEl.innerHTML = `<span>${esc(run.run_id)}</span>
      ${run.parent_run_id ? `<span>child of ${esc(run.parent_run_id)}</span>` : ''}
      <span>${esc(run.model || '')}</span>
      <span>${pill(run.status)}</span>
      <span>${esc(run.title || 'untitled session')}</span>
      <span>started ${new Date(run.started_at * 1000).toLocaleString()}</span>`;
  });

  source = new EventSource(`/api/runs/${runId}/stream`);
  source.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    logEl.append(render(ev));
    logEl.scrollTop = logEl.scrollHeight;
  };
  source.addEventListener('end', () => { source.close(); loadRuns(); });
  source.onerror = () => { /* EventSource retries; the generator says why it ended */ };
}

loadRuns();
</script>
</html>
"""
