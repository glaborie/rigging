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

MLflow landed in v0.6 as a run-level backend over its REST API (ADR-0020): two
interchangeable backends with no vendor SDK in the loop, and span-level
attributes that already satisfy Langfuse's ingestion contract (verified previously
against cloud.langfuse.com). Cost: `gen_ai.*` conventions are still moving, so
attribute names are centralised in one module.

## Alternatives rejected

Langfuse SDK directly: rejected — exactly the coupling that already cost a
migration. Own JSONL event stream with adapters later: kept as the store, not as
the tracing interface.

## Postscript (v0.6.3): trace tags are a setting, not a protocol

A benchmark produces one trace per eval case, so "which model, which pass?" has
to be filterable in the UI. Langfuse maps `langfuse.trace.tags` (string[]) to
trace tags; `RIGGING_TRACE_TAGS` (comma-separated) is read in `loop._trace_tags()`
and set on the root span only. `scripts/bench.py` writes `bench,<model>,rep-<n>`
per rep, so the same code path serves a demo run and a 40-trace benchmark.

Cost: one more environment variable, and an attribute that is invisible until
you open the trace. It is deliberately not a manifest field — tags describe the
experiment, not the agent, so a manifest must not hard-code them. Tags are
labels, not content: `capture_content: false` still sends them. Verified on
cloud.langfuse.com — trace tags `["bench","gemini/gemini-3.7-flash","rep-1"]`.
