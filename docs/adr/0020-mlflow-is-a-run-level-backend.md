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

## Postscript: the target is validated at assembly (2026-11-05)

The run-level mapping above has a failure mode the rest of the design hides. The
loop swallows exporter errors by design (ADR-0012), so if `MLFLOW_EXPERIMENT_ID`
names an experiment that does not exist — or a *deleted* one, which is easy: MLflow
never reuses an id, so deleting and re-creating an experiment leaves the old id in
`.env` — then `runs/create` returns `RESOURCE_DOES_NOT_EXIST` mid-run, nobody sees
it, and a 85-run benchmark reports `ok` with nothing recorded.

"Exporter errors never fail a run" is a rule about the run, not about startup. The
registry entry for `mlflow` is therefore a factory (`mlflow_exporter`) that resolves
the experiment (`GET /api/2.0/mlflow/experiments/get`) and raises
`ExporterConfigError` if it is missing, deleted or the server is unreachable. The
loop assembles exporters before the first model call, so the command exits 2 with
one actionable line. `MLflowExporter.__init__` stays I/O-free so the class remains
constructible offline in tests.

Cost: one extra HTTP GET per run in an eval suite (17 cases = 17 GETs against the
tracking server), and `--export mlflow` now refuses to start when the backend is
down instead of running bare. That is the intended trade: a benchmark that cannot
export is a configuration error, not a degraded success.

Rejected: creating the experiment on demand. A benchmark should write to a target
a human chose, not invent one, and a typo'd id would then silently create
`experiment-1` beside the real one.
