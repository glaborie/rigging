# Examples

## `b2b-support/` — the canonical demo agent

Target state: v0.3, shipped. The manifest, prompt, both skills, the tools and the
seeded fixture database are final (`seed.py` documents the shape).

It exercises every layer of the harness at once:

| Layer | How this example exercises it |
|---|---|
| tools | 11 tools, one of them `side_effecting` (`refund_order`) |
| registry | all capabilities resolved by name from the manifest |
| skills | two skills, index-only in the prompt, body fetched by `load_skill` |
| retrieval | policies via FTS5 (`search_policies`; Qdrant is the upgrade path) |
| guardrails | PII on input/output, injection on tool results, approval threshold, commitment check |
| approval | `refund_order` cannot run without an operator answer |
| escalation | `escalate_to_human` produces a structured handoff |
| observability | spans per step, exported to Langfuse |

## `hello/` — the v0.1 smoke test

No tools, no retrieval, one prompt. Use this to verify a gateway is wired
correctly before blaming the demo agent.

## `subagents/` — delegation as a child run (v0.6)

`agent.yaml` declares one subagent (`researcher.yaml`) and gets a
`spawn_researcher(task)` tool. The child is a **run**, not a subprocess: same
event log, same session, `runs.parent_run_id` set, its own model, prompt and
budget (ADR-0019).

```bash
uv run harness run examples/subagents/agent.yaml \
  "Use the researcher to explain what a vector database is, then summarise it in one sentence." --yes
uv run harness runs          # the child is indented under the parent
make serve                   # both runs, the child labelled "child of …"
```

`--yes` is needed because `spawn_*` tools are side-effecting: delegation spends
tokens, so it goes through the same approval gate as a refund. The child never
sees the parent's conversation — only the task string.

## `parallel/` — two tools in one response (v0.6)

`parallel_tools: true` runs only `tool.run` for a single model response in a
bounded pool; prepare (events, spans, guardrails, approval) and finish (events,
messages) stay serial and in declared order (ADR-0022).

```bash
uv run harness run examples/parallel/agent.yaml \
  "In one response, call current_time for Europe/Zurich and sha256_hex for the text 'rigging'."
uv run harness runs --run <run_id> | grep tool_batch
# tool_batch {"parallel": true, "calls": ["current_time", "sha256_hex"]}
```

Flip the flag to `false` and the same prompt records `{"parallel": false}`.

## `mlflow/` — one run per run (v0.6)

The same loop with the MLflow exporter instead of Langfuse. No SDK and no extra:
the exporter speaks MLflow's `2.0` tracking REST API (ADR-0020).

```bash
uvx mlflow server --backend-store-uri sqlite:///mlflow.db --port 5000
uv run harness run examples/mlflow/agent.yaml "say hello in one sentence"
```

The MLflow run is `FINISHED` on `ok`, `FAILED` on a denied or exhausted run.

## `observability/` — spans to Langfuse

Same shape as `mlflow/`, with `exporters: [langfuse]` over OTLP. `make trace` is
the one command that runs it and reads the spans back.
