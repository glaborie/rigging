# ADR-0029: An approval can outlive the run that asked for it

**Status:** Proposed — not implemented yet; this page is the sign-off for hard
rule 6. Amends ADR-0007 (approval is a policy call inside the loop) and ADR-0023
(the page's approval channel) without repealing either.

## Context

Every approval in the harness is a **blocking call on the loop's thread**:

- `CLIPromptApproval.request()` blocks on `input()`.
- `WebApproval.request()` blocks on a `threading.Event` for
  `APPROVAL_TIMEOUT_S` (600s), then returns a denial.
- `AutoDenyApproval` does not block because it never asks.

That design is honest for a demo and wrong for an operator. It ties the decision
to a live thread, which produces four failures we can name:

1. **The approver has to be watching.** A reviewer at lunch is a denial after ten
   minutes, recorded with the same note as someone who said no.
2. **A restart loses the request.** Nothing durable says "this run is waiting",
   so `harness runs` shows a run that simply stopped, and the only recovery is
   re-running the prompt — which re-executes every tool that already ran.
3. **A non-page client cannot answer.** ADR-0028's `/v1` bridge denies side
   effects precisely because that channel has no approval, and the backlog's
   "approval channel for non-page clients" has nowhere to put the request.
4. **The run and the decision have different lifetimes.** The decision belongs to
   a person who may be on another machine, an hour later. The loop holds it.

The event log already has most of the parts: `approval_requested` and
`approval_verdict` are written by the loop, `EventLog.messages(run_id)` already
projects the conversation as the model saw it, and `EventLog.usage(run_id)`
already recomputes spend. What is missing is a way for the loop to *stop* at an
approval and for a later process to *resume* from the log instead of from a
coroutine frame.

## Decision

A run may end in a new terminal status, `pending_approval`. Ending and resuming
are both the loop's job; nothing outside it decides anything.

- **The suspension point is the policy.** A new `ApprovalPolicy`,
  `deferred` (selectable as `approval.policy: deferred`), raises `ApprovalPending`
  instead of answering. The loop catches it in `_prepare_call`, writes
  `approval_pending` with `{approval_id, call_id, tool, arguments, reason,
  expires_at}`, and lets it propagate. **The tool has not run**, and by hard rule
  4 it cannot have: approval is checked before `_run_tool` on every path,
  including the parallel one.
- **A pending approval is an event, not a row.** `approval_id` is
  `ap_<hex>` and lives in the log. There is no second table and no server-side
  dict (ADR-0005); `harness approvals` is a projection, like `escalations`.
- **`RunResult.status` gains `pending_approval`** and `RunResult.pending` carries
  the request to the caller. Exporters close on this path like every other, so a
  suspended run produces a complete trace with `rigging.status =
  pending_approval`.
- **Resume is `resume_run(spec, run_id, grant)`.** It refuses unless the run is
  `pending_approval`, an `approval_pending` exists, no `approval_verdict` for that
  id exists, and the stored arguments still hash to what the grant was issued
  against. It then appends `approval_verdict` (with the approver label), writes
  `run_resumed`, rebuilds `[system, *log.messages(run_id)]`, and continues the
  loop from the first unanswered tool call of the last `model_response` — same
  `run_id`, same session, same usage. **Prior tools are not re-executed**; that is
  the whole point.
- **`on_tool_call` runs again on resume, deliberately.** The post-transform
  arguments are not in the log (ADR-0025: a guardrail may restore a PII sentinel,
  and the log is the last place it may appear), so resume re-runs the chain to
  recover what would have been executed. The verdict is written with
  `resumed: true` and the same call id, so the log reads as one decision made
  twice by the same chain, not two independent decisions.
- **Silence expires into a denial, never into an approval.** `expires_at` defaults
  to 24h (`RIGGING_APPROVAL_TTL_S`). Resuming an expired approval refuses and
  records `approval_verdict{approved: false, note: "expired"}` — the same posture
  as ADR-0023's ten-minute timeout, for the same reason.
- **One decision per approval, enforced by a transaction.** The check for an
  existing `approval_verdict` and the append happen under `BEGIN IMMEDIATE`, so
  two clients racing on the same `approval_id` produce one verdict and a 409, not
  two refunds.
- **Who may answer: the channel, and this is not authentication.** The harness has
  no user accounts. An approver is a **self-declared label** recorded in the
  verdict, and the authority to answer is possession of the channel: the bearer
  token for `POST /api/approvals/{id}` (ADR-0021), the terminal for
  `harness approve`. The ADR says so out loud, `docs/threat-model.md` gets the
  same sentence, and no doc claims the label is a verified identity. Per-user
  authority is a later ADR; the demo has no caller identity to bind to (which is
  why the red team excludes `bola`/`rbac`).
- **Scope: root runs only.** A suspending subagent would need the parent's
  suspension to compose with it, which is a different problem. `spawn_*` stays
  approval-gated and blocking; resume refuses a run with `parent_run_id` set.
- **The page keeps its blocking channel.** `WebApproval` stays for the live demo
  (the beat where you click "refund" and watch it happen). The new surface is for
  clients that were never there when the run stopped: `GET /api/approvals`,
  `POST /api/approvals/{id}`, `harness approvals`, `harness approve <id>
  [--deny] [--as NAME]`.

## Consequences

A run now has a state that outlives its process, so every place that enumerates
run statuses has to learn one more: the `Literal` in `RunResult`, `harness runs`,
the web view's status column, and `docs/architecture.md` §3. That is the price of
the feature and it is worth paying once, deliberately.

Cost, honestly:

- **Two traces, one run id.** Resume starts a new root span with the same
  `rigging.run_id` and `rigging.resumed=true`. Langfuse shows two observations
  for one run; the alternative is holding a span open across a process boundary,
  which OTLP cannot do.
- **The loop gets refactored.** The body from the step loop to the final answer is
  extracted so `run_agent` and `resume_run` share it. That is the riskiest edit in
  the change, and the existing suite is the guard: if 374 offline tests still pass
  and the new resume tests fail without the feature, the refactor was mechanical.
- **`pending` is durable, so it can rot.** Approvals nobody answers accumulate;
  `harness approvals` has to show age, and the TTL only acts when someone tries
  to resume.
- **The bearer token gets more authority.** It can now release a refund that was
  requested hours ago, not just answer one that is blocking. `docs/threat-model.md`
  needs that in the token's entry, not implied.

## Alternatives rejected

- **Keep blocking, just raise the timeout.** Moves the problem; a longer wait is
  still a wait, and a restart still loses the request.
- **Re-run the prompt on resume.** Simpler and wrong: it re-executes every tool
  that already ran. A duplicated `refund_order` is the exact failure the approval
  system exists to prevent.
- **Store loop state as a pickled frame or a server-side dict.** A second source
  of truth (ADR-0005), invisible to `harness runs`, lost on restart, and
  impossible to redact.
- **Let the service append `approval_verdict` itself.** The decision would be made
  outside the loop, which is ADR-0007's line: enforcement is the loop's job, and
  a service that writes verdicts is a second policy engine.
- **Treat the approver label as an identity.** It is not verifiable without
  accounts, and claiming otherwise would be the kind of sentence this repo
  forbids: user-facing text must not claim what the code does not do.
- **Auto-approve when the request expires.** An expiry that approves is a
  time-delayed bypass of hard rule 4.
