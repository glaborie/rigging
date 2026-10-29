# ADR-0011: Cost is computed from a static price table times gateway usage

**Status:** Accepted

## Context

`max_cost_usd` needs prices. Gateways report token `usage` reliably; they do not
report cost uniformly. Tokenizers differ per provider, so local counting is both
extra work and wrong.

## Decision

`prices.yaml` ships with the package (`model -> {input, output}` USD per 1M
tokens), overridable via `prices_file`. Cost per step = `usage` × price, summed
across the run. Unknown model ⇒ a `cost_unknown` event and the USD limit is
skipped (step/token/wall-clock limits still apply). No `tiktoken` anywhere.

## Consequences

Cost tracking with no tokenizer dependency, correct for models the gateway
knows. Cost: prices are a maintenance burden and can go stale — the unknown-model
path exists so staleness degrades safely instead of silently mis-billing.

## Alternatives rejected

`tiktoken` estimation with pre-call abort: genuinely useful, deferred as a
documented improvement (it cannot be correct across Ollama/OmniRoute models).
No cost tracking at all: rejected — the eval table's cost column and a
cost-aware portfolio claim both depend on it.
