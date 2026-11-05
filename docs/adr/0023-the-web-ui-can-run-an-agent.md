# ADR-0023: The web UI can run an agent, with approvals in the page

**Status:** Accepted. Supersedes the "write-capable web UI" clause of ADR-0017 and
the "no endpoint that writes, launches or approves" clause of ADR-0018. Partly
superseded by ADR-0028: its "OpenWebUI / AnythingLLM / LibreChat" rejection is
reversed for an opt-in OpenAI-compatible front door. The page and its approval
channel remain the only place a human answers a side-effecting tool.

## Context

ADR-0018 shipped a projection of the event log on purpose: no write path, so the
page can never disagree with the CLI. That was the right call for a debugging
window, and it left the project without a demo a reviewer can drive. Watching a
recorded run is not the same as typing "refund ORD-1002" and seeing the loop
look up the order, refuse the refund, and ask for approval.

The ask is real: "do I need OpenWebUI or AnythingLLM?" No — those are
OpenAI-compatible *clients*, and rigging is a client of such endpoints, not a
server that exposes `/v1/chat/completions`. Pointing them at the gateway chats
with a raw model: no tools, no guardrails, no approvals, no event log. The only
honest way to demo the harness is a page that starts a harness run.

That last sentence is what ADR-0028 revisits: the surface exists now, opt-in, and
the approval argument below is why it denies side effects instead of pretending to
ask.

Two constraints survive from ADR-0018 and are not negotiable:

1. **The loop stays the only thing that runs an agent.** A page that reimplements
   the loop would drift from it immediately.
2. **The event log stays the only source of truth.** Multi-turn history is a
   projection over the log, not a dict in the server process.

## Decision

`harness serve --manifest <path>` adds a chat endpoint. Without the flag the
service is exactly what ADR-0018 shipped, route for route.

- **One run, one `run_agent`.** `POST /api/chat` starts `run_agent` in a worker
  thread and returns `{session_id, run_id}` immediately. The page then opens the
  existing `/api/runs/{run_id}/stream` SSE route, so there is no second streaming
  implementation.
- **The run id exists before the first event.** `run_agent` and
  `EventLog.start_run` accept an explicit `run_id` (insert is idempotent), so the
  page can subscribe without racing the worker thread.
- **History is a projection.** `EventLog.session_messages(session_id)` replays the
  session's root runs in order. A server restart loses nothing; a second tab sees
  the same conversation.
- **Approvals are a channel, not a policy.** `WebApproval` is an `ApprovalPolicy`
  that blocks the loop thread on a `threading.Event` until
  `POST /api/runs/{run_id}/approval` answers. The loop still writes
  `approval_requested` and `approval_verdict`; the page reads the first and
  answers with the second. No answer within ten minutes is a denial. The page
  replaces **only** `policy: cli` — the interactive policy it stands in for.
  `auto_deny` and `scripted` are deliberate unattended choices, so a server flag
  cannot widen them; the page shows the request and the denial, without buttons.
- **Writes need a bearer token, always.** Even on loopback. `create_app` refuses
  to build a chat-enabled app without one. Two reasons: a page on any origin can
  POST to `127.0.0.1:8000`, and the token is the one thing an attacker's page
  cannot read. A cross-origin JSON POST also needs a CORS preflight the app never
  grants, so the two controls are independent.
- **One turn at a time per session.** A second `POST` while a run is still
  `running` returns 409 instead of interleaving two histories.

## Consequences

The page now has a write path, so the threat model grows: a token becomes
mandatory for chat, the rate limiter covers `/api/*` unchanged, and the approval
channel is a second place a decision enters the system. That is acceptable
because the decision is still made by the loop's policy object and logged by the
loop — the page only carries the answer, exactly as the terminal prompt does.

Cost: a thread per turn (no queue, no worker pool — a demo runs one turn at a
time), a 409 path the CLI does not have, and one more module for `mypy --strict`
to hold. Multi-user, multi-session chat is not attempted; the token is a shared
secret, not an identity.

## Alternatives rejected

- **OpenWebUI / AnythingLLM / LibreChat**: they speak OpenAI's chat-completions
  protocol to a server. rigging is not a server, and approvals need a channel
  those clients do not have. Wiring them up would demo a model, not the harness.
  Revisited in ADR-0028: the protocol is served behind `--openai-api` now, with
  side effects denied because that channel is still missing. This page remains the
  only surface that shows an approval as a decision.
- **Running the loop in the browser**: a second implementation of the thing this
  repository exists to demonstrate.
- **WebSocket for approvals**: SSE down plus a plain POST up is smaller, and the
  approval is one request/response, not a stream.
- **Auto-approve in demo mode**: removes the one beat that shows hard rule 4. A
  demo that cannot show a denial is not showing the harness.
- **Keeping chat in a server-side history dict**: drifts from the log on restart,
  and ADR-0005 already answered where conversation state lives.
