# ADR-0009: Laya is an advisory decision model; deterministic checks remain the gate

**Status:** Accepted

## Context

Two guardrails need judgement: "does this reply promise something outside
policy?" and "is this request too vague or too complex, escalate?". An LLM judge
costs a second generation, is unmeasurable, and can be prompt-injected.
`convaiinnovations/laya` is a 421M non-autoregressive decision model: typed
questions in, calibrated probabilities out, ~33ms, no generation to parse, and it
ships an act/escalate head plus a typed-decisions checkpoint covering customer
service.

## Decision

`DecisionModel` protocol in core. `LayaDecisionModel` behind the `rigging[laya]`
extra, used as an *advisory* signal: `p_bad > threshold` flags/escalates. The
deterministic regex/allow-list check stays the hard gate in v1. A ~40-case labelled
set in `tests/data/decisions.jsonl`, scored by the eval layer, decides whether Laya
ever earns gate authority.

## Consequences

A second, cheap, non-generative opinion that cannot hallucinate and whose
calibration is measurable on our own distribution. Cost: a heavy optional extra
(torch + transformers) and an honest caveat — Reflection's own README says Laya is
"a fast base to specialise, not a zero-shot decision engine" (0.362 vs 0.766
accuracy on their typed-decisions benchmark), so its authority must be earned by
measurement, not assumed.

## Alternatives rejected

LLM-as-judge guardrail: rejected — cost, latency, attack surface, unmeasurability.
Deterministic-only: honest but blunt on paraphrase. Fine-tuning Laya: a non-goal
for v1.
