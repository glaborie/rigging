# ADR-0018: The web widget is a read-only view of the event log

**Status:** Accepted (supersedes the "no web UI before the v0.5 stretch" clause of
ADR-0017; its no-auth clause is superseded by ADR-0021)

## Context

v0.5 asks for a FastAPI + SSE widget. Two failure modes are worth naming before
writing it:

1. **A second source of truth.** The event log is authoritative (ADR-0005); the
   moment a UI keeps its own run state, drift is guaranteed and the log stops
   being the thing a debugger can trust.
2. **A second write path.** Approvals are enforced in the loop (hard rule 4). A UI
   that can also approve or launch runs duplicates the security boundary, and the
   two copies will disagree.

There is also a dependency question: `fastapi` and `uvicorn` must not become core
dependencies for people who only run agents on the CLI.

## Decision

Ship `rigging/service/` behind the existing `[service]` extra:

- **Read-only.** The app exposes sessions, runs and their events. There is no
  endpoint that writes, launches or approves. Approvals stay in the loop, where
  the policy already lives.
- **The event log is the only input.** Every response is a query over `sessions`,
  `runs` and `run_events`; nothing is cached and no second store exists. Payloads
  are whatever the redactor wrote (hard rule 5) — the service never re-reads a raw
  payload, because there is none to read.
- **SSE, not WebSocket.** Live updates are `text/event-stream` from an in-process
  generator that tails `run_events` by `seq`. SSE needs no protocol upgrade and
  works with `curl`. (ADR-0021 later moved the page to `fetch` +
  `ReadableStream` so it can send a bearer header; the wire format is unchanged,
  and the page no longer reconnects on its own.)
- **No build step, no CDN.** One HTML page with vanilla JS is served from the same
  app. A reviewer can read the whole UI in a minute and there is no `node_modules`
  in a Python portfolio piece.
- **Local by default, no auth (superseded by ADR-0021).** `harness serve` binds
  `127.0.0.1`. The service is a development window onto a local database;
  exposing it is a deployment decision that would need its own ADR (the v0.6
  service + auth milestone). ADR-0021 added a bearer token for that case.
- **Optional dependency, honest failure.** `harness serve` without the extra
  prints the `uv sync --extra service` hint and exits 2 (hard rule 9).

## Consequences

The widget is a projection, so it can never disagree with the CLI: deleting it
would lose no information. Cost: no interactivity beyond reading (deliberate — see
above), a polling interval of a few hundred milliseconds inside the SSE generator
rather than push notifications, and one more module that mypy must keep honest.

## Alternatives rejected

- **WebSocket + a JS client store**: a second cache and a protocol upgrade for
  data that is already append-only.
- **HTMX or a JS framework**: a build step and a dependency tree to render a list
  and a log.
- **A write-capable UI** (launch runs, answer approvals): duplicates the loop's
  approval policy; would need an auth story before it could be safe.
- **A separate metrics database for the UI**: the event log already answers every
  question the page asks.

## Postscript (2026-11-03): the page never rendered

The widget shipped in v0.5 and was re-verified in v0.6 with "page 200" — and the
page rendered nothing. `INDEX_HTML` was a normal Python string, so `'\n\n'` in the
inline JavaScript became a **real newline inside a single-quoted string**. The
browser rejected the whole script, and the page sat on "loading runs…" while
`/api/runs` answered 200. The route tests passed because they read the page as
text; none of them parsed it, and the evidence line checked the shell, not the
render.

Three changes came out of it:

- `INDEX_HTML` is a raw string, so JS escapes survive the trip through Python.
- A failed `loadRuns()` reports the failure instead of leaving the placeholder up.
- `tests/test_service_browser.py` loads the page in headless Chromium and asserts
  the run list renders, with a page-error listener. It is opt-in (`make
  test-browser`, `[browser]` extra) and runs as its own CI job.

The lesson is recorded here rather than in a commit nobody reads: for a UI, a 200
is not evidence that it works. A route test proves the bytes were sent; only a
browser proves they ran.
