# ADR-0004: Guardrails are four named hooks, not a generic middleware stack

**Status:** Accepted

## Context

A guardrail has to be able to look at inputs, at tool calls before they execute,
and at outputs before they reach the user. A generic middleware stack can express
all three and infinite more, at the cost of making the common case unreadable and
the evaluation of "what protects this agent" impossible to answer.

## Decision

`Guardrail` exposes exactly four hooks: `on_input`, `on_tool_call`, `on_tool_result`, `on_output`. Each returns
`Allow`, `Deny(reason)` or `Transform(replacement)`; chains compose left to right,
first `Deny` wins, `Transform` feeds the next guardrail. The `on_tool_call` hook
receives the `Tool` object, so it sees `side_effecting` and the JSON Schema.
Escape hatch: a documented full-iteration hook is reserved for v0.6 if a real need
appears.

**Correction discovered while implementing the built-ins:** the protocol started
with three hooks. Writing the prompt-injection heuristic proved that retrieved
policy text, MCP results and third-party API responses are untrusted *input to the
model*, and inspecting them requires a fourth hook that is neither the tool call
nor the final output. `on_tool_result` was added before v0.1 shipped — four hooks,
one correction, still no generic middleware.

## Consequences

The manifest can state exactly which protections run where, and an eval can assert
on verdict events. Cost: a genuinely novel interception point needs a core change
rather than a plugin.

## Alternatives rejected

Generic middleware: rejected for legibility. Single `validate()` entry point:
cannot express pre-execution tool inspection, which is where the interesting
guardrails live.
