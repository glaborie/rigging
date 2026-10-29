# ADR-0012: Observability is OpenTelemetry spans with Langfuse as the first backend

**Status:** Accepted

## Context

Langfuse v4→v5 removed `set_trace_io()` and moved to root-observation
`update()`, which broke the Hermes Langfuse plugin. The lesson generalises: a
harness must not import its observability vendor. MLflow is also in play for
experiment tracking.

## Decision

Emit spans with OTel `gen_ai.*` semantic attributes over OTLP. Langfuse
(`.[langfuse]` extra) is the first exporter, MLflow the second, both as `Exporter`
implementations. Session id on the root span plus each generation; I/O,
model and usage as span attributes. The event log stays the local source of
truth; spans are the distributed view.

## Consequences

Two interchangeable backends with no vendor SDK in the loop, and span-level
attributes that already satisfy Langfuse's ingestion contract (verified previously
against cloud.langfuse.com). Cost: `gen_ai.*` conventions are still moving, so
attribute names are centralised in one module.

## Alternatives rejected

Langfuse SDK directly: rejected — exactly the coupling that already cost a
migration. Own JSONL event stream with adapters later: kept as the store, not as
the tracing interface.
