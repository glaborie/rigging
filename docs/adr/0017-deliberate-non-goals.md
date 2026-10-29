# ADR-0017: Deliberate non-goals

**Status:** Accepted

## Context

A harness can grow in every direction at once. Every unbuilt feature is a decision
that has to be written down or it will be re-litigated, half-implemented, or
mistaken for an oversight by a reviewer.

## Decision

The following are explicitly out of scope, stated in the README: agent long-term
or vector memory; multi-agent orchestration in v1 (`SubagentRunner` is a protocol
slot with `parent_run_id` pre-wired only); serving MCP; LLM-as-judge guardrails or
evals; local tokenizer estimation and pre-call cost aborts; user auth before the
service milestone; a web UI before the v0.5 stretch; model fine-tuning; parallel tool
execution; Windows-native support (WSL/Linux first); a third-party plugin
marketplace beyond entry points themselves.

## Consequences

Scope stays finishable, and the README reads as deliberate rather than
incomplete. Cost: some obvious asks ("add memory") get answered with a link.

## Alternatives rejected

Building memory, subagents and a UI early: rejected — each multiplies every
unsolved problem in the loop. Hiding the list: rejected, it invites the same
question every review.
