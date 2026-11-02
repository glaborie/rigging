# ADR-0018: The web widget is a read-only view of the event log

**Status:** Accepted (supersedes the "no web UI before the v0.5 stretch" clause of ADR-0017)

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
  generator that tails `run_events` by `seq`. SSE needs no protocol upgrade, works
  with `EventSource` and `curl`, and the browser reconnects on its own when a
  still-running run outlives the connection.
- **No build step, no CDN.** One HTML page with vanilla JS is served from the same
  app. A reviewer can read the whole UI in a minute and there is no `node_modules`
  in a Python portfolio piece.
- **Local by default, no auth.** `harness serve` binds `127.0.0.1`. The service is
  a development window onto a local database; exposing it is a deployment decision
  that would need its own ADR (the v0.6 service + auth milestone).
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
