# ADR-0001: The gateway contract is OpenAI-compatible HTTP, not a vendor SDK

**Status:** Accepted

## Context

The harness must work against OmniRoute, Ollama, OpenRouter, vLLM, LM Studio and
anything else the user points it at. LiteLLM was the obvious normalisation layer,
but its release pipeline was compromised on 21 Oct 2026 (`litellm==1.107.0`, a
supply-chain compromise that executed on interpreter startup) and it has carried
repeated proxy CVEs. Its dependency weight also dwarfs a harness that is supposed
to be a harness.

## Decision

One canonical internal request/response type set, translated to and from
`POST /v1/chat/completions` by a single `OpenAICompatClient` built on `httpx`.
The `openai` Python SDK is not a dependency. SSE parsing is hand-written and
covered by recorded wire fixtures. The Responses API is a second `LLMClient`
implementation when it earns its place, not a day-one requirement.

## Consequences

Zero vendor SDK surface, zero compiled dependencies, and gateway switching is a
base URL. Cost: we own the SSE parser and must track provider drift; mitigated by
contract tests over recorded fixtures and a `sse_malformed` event instead of a
crash. Provider-specific features (Anthropic prompt caching, Gemini thought
signatures) need adapter work when we want them.

## Alternatives rejected

LiteLLM as the transport: rejected on supply-chain risk and dependency weight.
The `openai` SDK as transport: rejected because the whole point is to show the
wire format is not magic. Direct per-provider adapters: rejected as five times the
work for the same demo value.
