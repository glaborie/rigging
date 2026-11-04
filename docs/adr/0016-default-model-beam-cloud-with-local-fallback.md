# ADR-0016: Default model is `beam:cloud`, with `qwen3:8b` as the local path

**Status:** Amended 2026-11-04 (see "Amendment" below) — the demo default is now
`deepseek/deepseek-v4.1-flash` via the `openrouter` profile.

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

## Amendment (2026-11-04): demo default is DeepSeek

**Context.** `beam:cloud` stopped being a usable default. In session
`s_d49ce8fe5699` it answered twice with `finish_reason: stop`, no text and no
tool calls (5 output tokens, then 150 reasoning tokens) — the loop now retries
once and fails the run, but a model that drops answers is the wrong first
impression. Ollama also retired its hosted DeepSeek models (`deepseek-v3.1`,
`deepseek-v3.2`) on 2026-07-15, so "DeepSeek through Ollama" is no longer a
route. The `openrouter` profile and its key were already in `.env`, and
`deepseek/deepseek-v4.1-flash` calls tools reliably.

**Decision.** `examples/b2b-support/agent.yaml` pins
`model: deepseek/deepseek-v4.1-flash` and `credential_profile: openrouter`.
`harness run` gains `--profile`, matching `eval`, `chat` and `serve`, so the
pair is overridable per invocation (`MODEL=` / `PROFILE=` from `make`).
`qwen3:8b` with `PROFILE=ollama` stays the fully local fallback.

**Consequences.** The canonical demo is no longer zero-credential: it needs
`RIGGING_PROFILE_OPENROUTER_API_KEY` in `.env`, and runs cost money at DeepSeek's
list rate (now in `prices.yaml`, so the USD cap applies). The smoke agent and the
replay path remain offline. `beam:cloud` and `qwen3:8b` still work as explicit
overrides; the model is named in the manifest, never in loop or tool code.
