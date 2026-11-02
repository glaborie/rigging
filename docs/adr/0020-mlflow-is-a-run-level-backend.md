# ADR-0020: MLflow is a run-level backend, spoken over its REST API

**Status:** Accepted (extends ADR-0012)

## Context

ADR-0012 promised two interchangeable exporters and made Langfuse the first. The
second has to answer a question Langfuse hides: Langfuse ingests spans, MLflow
tracks runs. There is no one-to-one mapping, and the obvious shortcut — import
the `mlflow` SDK and call `start_run()` from inside the loop — would put a
vendor's heavyweight dependency in the path of every agent run.

## Decision

`MLflowExporter` maps **one rigging run to one MLflow run**:

| rigging | MLflow |
|---|---|
| root `invoke_agent` span start | `POST /api/2.0/mlflow/runs/create` (tags: session, run id, agent, model) |
| child spans (`chat`, `execute_tool`) | counted and summed, not stored individually |
| root span end | `POST /api/2.0/mlflow/runs/log-batch` (params: model/spec/status; metrics: tokens, steps, latency, cost, call counts) |
| root span end | `POST /api/2.0/mlflow/runs/update` (`FINISHED`, or `FAILED` when the run did not end `ok`) |

Two consequences of that mapping are deliberate:

- **No SDK, no new dependency.** MLflow's tracking server is HTTP, so the exporter
  uses `httpx` (already core) and the documented `2.0` REST API. There is no
  `[mlflow]` extra because there is nothing to install. Auth is
  `Authorization: Bearer $MLFLOW_TRACKING_TOKEN` when a token is configured.
- **Child spans become numbers, not spans.** MLflow is an experiment tracker:
  "how many chat calls, how many tokens, how long" is what it is good at. Anyone
  who needs per-span detail reads the event log or Langfuse; the exporter does not
  pretend to be a second trace viewer.

The loop had to change for this to be true: the root span is now **ended** on
every exit path (`ok`, `denied`, `budget_exhausted`, output-deny). Previously only
the ok path ended it, so a run-level backend would have shown a permanently
`RUNNING` run for every denied or exhausted run.

## Consequences

One backend that survives without the vendor's SDK and works with any MLflow
tracking server (local file store served by `mlflow server`, or a hosted one).
Cost: no span-level fidelity in MLflow, child spans are aggregated rather than
queryable, and metric names are a small vocabulary we now own
(`gen_ai.usage.*`, `rigging.*`) — the same names the OTel exporter uses, so a
dashboard can read either backend.

## Alternatives rejected

- **Import the `mlflow` SDK**: ~200 MB of dependencies and a second tracing model
  for a backend that only needs four HTTP calls.
- **One MLflow run per span**: unusable UI (a run per tool call) and it abuses
  runs as spans.
- **MLflow 3 trace ingestion (OTLP)**: closer in spirit, but the ingestion
  contract is younger than the tracking API and would tie us to one MLflow major
  version.
