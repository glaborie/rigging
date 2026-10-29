# ADR-0007: Escalation is a tool; consequence is tool metadata consumed by guardrails

**Status:** Accepted

## Context

A support agent must (a) know when to hand off to a human and (b) never issue a
refund on its own authority. These are different mechanisms that are often
conflated: (a) is a decision the model makes, (b) is a constraint the harness
enforces.

## Decision

`escalate_to_human(reason, summary, facts)` is an ordinary tool whose result is a
`Handoff` object ending the turn with a structured payload (transcript ids, reason,
summary) that a real deployment would POST to a ticketing system. Separately, `Tool`
carries `side_effecting: bool`; the loop routes any call to a side-effecting tool
through `ApprovalPolicy` (`CLIApproval` prompts y/N, `ScriptedApproval` answers
deterministically in tests and replay, `AutoDeny` is the default when unattended).
The `on_tool_call` guardrail also sees `side_effecting` and may deny outright.

## Consequences

Tools declare consequences, guardrails decide, the loop enforces. A refund demo
where the transcript *shows* the approval step is the project's strongest single
artifact. Cost: an unattended run cannot execute write actions without an explicit
policy, which is the intended behaviour.

## Alternatives rejected

Escalation-only with docstring warnings: rejected — warnings are not enforcement.
Approval-only: leaves handoff semantics to prose. A separate "agent handoff
protocol": premature for one agent.
