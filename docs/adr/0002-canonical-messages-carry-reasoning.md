# ADR-0002: Canonical message types carry reasoning and usage explicitly

**Status:** Accepted

## Context

The default model, `beam:cloud`, is a reasoning model with a 1M context window
and a `thinking` tag. Several gateways expose reasoning text in a side channel
(`reasoning_content`, `reasoning`, or vendor-specific fields) rather than in
`content`. A harness that drops it loses tokens it paid for, mis-accounts usage,
and cannot show the trace that makes an agent's behaviour explainable.

## Decision

`Message` and `ModelResponse` carry an explicit `reasoning: str | None`, and the
event log has `reasoning_delta` as a first-class event type alongside
`model_delta`. Usage is always a typed `Usage` object (`input_tokens`,
`output_tokens`, `cached_tokens`), never a bare int, so budget accounting has a
single source.

## Consequences

Reasoning is preserved for observability and never re-sent to the model unless a
gateway explicitly requires it (documented per-adapter). Cost accounting becomes
correct for reasoning models. Cost: one more field to map in every adapter, and
the CLI needs a `--show-reasoning` flag rather than dumping it by default.

## Alternatives rejected

Treating reasoning as part of `content`: pollutes transcripts and breaks
tool-call parsing. Dropping it entirely: cheap, but loses the single best
debugging artifact a reasoning model produces.
