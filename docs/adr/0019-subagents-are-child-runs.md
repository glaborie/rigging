# ADR-0019: A subagent is a child run in the same event log

**Status:** Accepted (supersedes the multi-agent clause of ADR-0017)

## Context

`SubagentRunner` has been a protocol slot since v0.1, with `runs.parent_run_id`
pre-wired and deliberately unused. v0.6 asks for child runs with their own
manifest and budget. Three things can go wrong, and each has a tempting shortcut:

1. **A second source of truth.** Running a child in its own process with its own
   log makes the parent's transcript a summary of something invisible.
2. **Unbounded recursion and cost.** A child that can spawn children that can
   spawn children is a fork bomb with an invoice.
3. **A guardrail bypass.** If the child is assembled from its own manifest, its
   guardrails are its own; if it is *dispatched* by the parent, approvals must
   still be the operator's decision, not the parent model's.

## Decision

A subagent is a **run**, not a process or a service:

- **Same log, same redaction.** The child appends to the parent's `EventLog` and
  gets `runs.parent_run_id` set. `harness runs` shows it as a child; the widget
  shows the same rows. Nothing about the child is summarised away.
- **Same session, isolated history.** The child shares the session (so one
  conversation groups the work) but starts with its own system prompt and the
  task string — it never inherits the parent's message history. Isolation is what
  makes a child's context budget its own.
- **Declared in the manifest.** `subagents: [{name, manifest, description}]`
  becomes one tool per child, `spawn_<name>(task)`. Paths resolve relative to the
  parent manifest, so the declaration is portable.
- **Side-effecting, therefore approved.** Every `spawn_*` tool is
  `side_effecting=True`: it spends tokens and time. The parent's
  `ApprovalPolicy` decides, exactly as it does for a refund.
- **The child inherits the parent's approval policy object**, not its own
  manifest's `approval.policy`. The operator answering a prompt is the same
  operator; a manifest cannot promote itself to `scripted`.
- **Depth is a runtime cap, not a manifest field.** `RIGGING_SUBAGENT_MAX_DEPTH`
  (default 1) is enforced by the runner, which returns a structured `is_error`
  tool result at the cap. The spawn tool still exists at depth, so the model is
  told why it cannot go deeper instead of silently losing a capability.
- **Budgets are per run.** The child enforces its own `limits`; the parent's step
  budget bounds how many times it can spawn. Child tokens are **not** folded into
  the parent's `usage` — the two runs may use different models, and pricing the
  child's tokens at the parent's rate would be a lie. The child's usage is in its
  own run row and in the tool result the parent sees.
- **Failure is a tool result.** Child crash, denial, budget exhaustion or a
  missing manifest becomes an `is_error` message the parent model can react to.
  Hard rule 3 applies recursively: a subagent can never crash a run.

## Consequences

The parent's transcript is complete enough to debug the child, and `parent_run_id`
makes the tree a query rather than a convention. Cost: no global cost ceiling
across a tree (each run caps itself; a shared budget object is a later addition),
child spans are separate traces linked by `rigging.parent_run_id` rather than
nested under the parent span, and a subagent adds a second full assembly of
tools, guardrails and exporters — cheap locally, not free.

## Alternatives rejected

- **Child process / separate log**: a second source of truth and a merge problem.
- **Inheriting the parent's message history**: the child would spend its budget
  re-reading a conversation it does not need, and prompt injection in the parent
  would flow straight into the child.
- **Child choosing its own approval policy**: a manifest could self-approve.
- **Folding child usage into the parent**: wrong-model pricing, and it hides which
  run actually spent the tokens.
- **A queue/worker pool**: that is a service concern, and v0.6 keeps the harness
  in-process.
