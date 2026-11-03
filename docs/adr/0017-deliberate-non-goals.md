# ADR-0017: Deliberate non-goals

**Status:** Accepted; partly superseded by ADR-0018 (web UI), ADR-0019
(subagents), ADR-0021 (service auth), ADR-0022 (parallel tool execution) and
ADR-0023 (a write-capable web UI).

## Context

A harness can grow in every direction at once. Every unbuilt feature is a decision
that has to be written down or it will be re-litigated, half-implemented, or
mistaken for an oversight by a reviewer. The list is also allowed to change: a
non-goal that later earns a milestone gets its own ADR and is removed here, so the
README never lists something the code now does.

## Decision

Still explicitly out of scope, stated in the README: agent long-term or vector
memory; serving MCP (ADR-0013 keeps consumption-only); LLM-as-judge guardrails or
evals (ADR-0009); local tokenizer estimation and pre-call cost aborts (ADR-0011);
model fine-tuning; Windows-native support (WSL/Linux first); and a third-party
plugin marketplace beyond entry points themselves.

### Superseded — built after all

These were on the original list and are now shipped. Each has an ADR and a
milestone; the README and this page record the change rather than pretending the
prediction was right.

| Original non-goal | Now | ADR |
|---|---|---|
| a web UI before the v0.5 stretch | read-only web view over the event log (v0.5) | ADR-0018 |
| multi-agent orchestration in v1 | subagents as child runs (v0.6) | ADR-0019 |
| user auth before the service milestone | bearer-token service auth (v0.6) | ADR-0021 |
| parallel tool execution | opt-in `parallel_tools` (v0.6) | ADR-0022 |
| a *write-capable* web UI | opt-in chat with in-page approvals (v0.7) | ADR-0023 |

The clause each superseding ADR replaces is named in its own Status line, so the
chain is readable in both directions.

## Consequences

Scope stays finishable, and the README reads as deliberate rather than
incomplete. Cost: some obvious asks ("add memory") get answered with a link, and
the list is now a moving target — every supersession has to be applied in three
places (this page, the README, and the milestone that ships it), which is exactly
where drift starts.

## Alternatives rejected

- **Building memory early**: rejected — memory multiplies every unsolved problem
  in the loop before the loop is trustworthy.
- **Hiding the list**: rejected, it invites the same question every review.
- **Freezing the list forever**: rejected once v0.5/v0.6 proved that a non-goal
  can become a milestone. Superseding it in public is cheaper than a README that
  quietly lies.
