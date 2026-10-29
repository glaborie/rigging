# ADR-0016: Default model is `beam:cloud`, with `qwen3:8b` as the local path

**Status:** Accepted

## Context

The demo lives or dies on tool-calling reliability. `beam:cloud` (Reflection,
501B MoE / 23B active, 1M context, `tools` + `thinking`) is free at the time of
writing and is the strongest tool-caller available without a paid key. It is a
cloud model served through Ollama, and "free" is a promotional state, not a
contract.

## Decision

`examples/b2b-support/agent.yaml` defaults to `beam:cloud` via the `ollama`
credential profile (`ollama signin`, no API key). `qwen3:8b` is the documented
fully-local alternative and the model the eval suite must also pass on. The model is
never hardcoded in code — only named in the manifest, overridable with `--model`
and with an `openrouter` profile documented for higher quality.

## Consequences

Zero-cost, high-quality default now, with no dependency on the promotion: when
`beam:cloud` stops being free, it is a YAML edit. Cost: `make hello` needs a free
Ollama account rather than being fully offline, so the README documents both paths
and "zero-credential" means zero *API key*.

## Alternatives rejected

`qwen3:8b` as the default: fully local, but weaker tool calling and a worse first
impression. Smallest viable model (`qwen3:4b`): fastest to pull, flakiest. Paid
OpenRouter default: rejected, a demo should not need a card.
