# ADR-0005: Run state is an append-only event log in SQLite

**Status:** Accepted

## Context

Replay, evaluation, tracing and debugging all need the same thing: an ordered,
durable record of everything that happened in a run. Normalised tables
(`messages`, `tool_calls`, `runs`) reconstruct order from timestamps and drift the
moment one write path forgets a column.

## Decision

One `run_events` table, `(run_id, seq, ts, type, payload, span_id, parent_run_id)`.
Messages, verdicts, usage and transcripts are queries/projections over it. Runs
and sessions are small bookkeeping tables. JSONL export feeds `--replay` and eval.

## Consequences

One write path, so no drift; guardrail denials, budget checks, escalations and
errors all become events with the same shape, and eval is predicate queries over
events. Cost: slightly more code to query, and payload JSON is not
schema-enforced per event type in v1 (validated by typed constructors instead).
Supersedes the earlier JSONL-file decision, which came before SQLite was chosen.

## Alternatives rejected

Normalised tables: rejected for order reconstruction and drift. Both in parallel
(events + normalised): rejected — the parallel write path is what always diverges.
JSONL files only: kept as export format, not as the store.
