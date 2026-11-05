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
and repeated on every span (v4 filters observations, not traces).
`scripts/bench.py` writes `bench,<model>,rep-<n>` per rep, so the same code path
serves a demo run and a 40-trace benchmark.

Cost: one more environment variable, and an attribute that is invisible until
you open the trace. It is deliberately not a manifest field — tags describe the
experiment, not the agent, so a manifest must not hard-code them. Tags are
labels, not content: `capture_content: false` still sends them. Verified on
cloud.langfuse.com — trace tags `["bench","opencode-go/deepseek-v4.1-flash","rep-1"]`.

Every trace also records the tools it used as `metadata.tools_called`, written
when the root span ends (the only point where the set is known). Langfuse's
native `calledToolNames` filter reads the `TOOL` observations themselves, so the
metadata is for exact-set queries rather than a tag workaround.

Trace-scope attributes (`session.id`, tags, user id) are repeated on every span,
not just the root. Langfuse aggregates and filters per observation: with
`session.id` only on the root, a session query returned a single row and
session-level filters (including tool names) saw nothing.

## Postscript (v0.8.1): the v4 observations-first migration

Langfuse Cloud retires the v3 read and write paths on 2026-11-16. The exporter
already sent OTLP with `x-langfuse-ingestion-version: 4`; the remaining work was
in the span shape and the read paths, not the transport:

- **Overall input/output lives on the root observation.** The legacy
  `langfuse.trace.input` / `.output` pair is no longer written: v4 has no trace
  entity, and its only remaining consumer would be a trace-level evaluator this
  repo does not have (ADR-0009).
- **Trace context is copied to every span**: `langfuse.trace.name`, tags,
  `session.id`, user id, release, version, environment and
  `langfuse.trace.metadata.*`. A filter on a child observation now sees them.
- **Every span names its `langfuse.observation.type`** (`agent`, `generation`,
  `tool`) instead of relying on inference, which keeps the ATIF projection
  stable.
- **Reads use `GET /api/public/v2/observations` exclusively.** A session is a
  `sessionId` filter grouped by `traceId`; every query is bounded by
  `fromStartTime`/`toStartTime` and follows `meta.cursor`. The deprecated
  `/api/public/traces` and `/api/public/sessions/{id}` endpoints are gone from
  the scripts.

Rejected: adopting the Langfuse Python SDK. It would undo this ADR's core
decision and pull `langfuse>=5` into the install. Raw OTel is the documented path
for non-SDK languages and is already vendor-free. Cost: the v4 attribute mapping
is ours to keep current, and `scripts/langfuse_to_atif.py` reconstructs trace
fields (`traceName`, `tags`) from `fields=trace_context` instead of a trace
object.
