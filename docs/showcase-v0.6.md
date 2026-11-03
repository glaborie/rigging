# Showcase v0.6 — four commands, four ADRs

v0.6 is scale-out, so the demo is four small proofs rather than one long run.
Each takes under two minutes and leaves evidence in a place a reviewer can
inspect: the event log, the run list, an HTTP status code, or an MLflow run.

Prerequisites:

```bash
make install                       # dev + service + mcp extras
ollama signin                      # beam:cloud; or `ollama pull qwen3:8b`
make check                         # 185 tests, ruff, mypy --strict
```

The commands below use `qwen3:8b` because the standing rule says the demo must
pass on the weakest documented model. `--model beam:cloud` works anywhere a model
id is accepted.

## 1. Subagents — delegation is a child run (ADR-0019)

```bash
uv run harness run examples/subagents/agent.yaml \
  "Use the researcher to explain what a vector database is, then summarise it in one sentence." \
  --yes
uv run harness runs
```

Expected: the parent run, then the child indented under it with `└`.

```
run_id             status       model                  started
2f6c…              ok           qwen3:8b               2026-11-03 10:12:04
  └ 8a41…          ok           qwen3:8b               2026-11-03 10:12:06
```

What it proves: the child is a **run in the same event log**, not a subprocess —
`runs.parent_run_id` is set, the child has its own model, prompt and budget, and
its tokens are counted in its own row, not folded into the parent's. `--yes` is
required because `spawn_researcher` is side-effecting and goes through the same
approval gate as a refund. Drill in with `uv run harness runs --run <child_id>`.

Also visible in the web view (step 3): both runs, child labelled as a child.

## 2. Parallel tools — opt-in, ordered, approved (ADR-0022)

```bash
uv run harness run examples/parallel/agent.yaml \
  "In one response, call current_time for Europe/Zurich and sha256_hex for the text 'rigging'. Then report both."
uv run harness runs --run <run_id> | grep tool_batch
```

Expected: one event whose payload is `{"parallel": true, "calls": ["current_time", "sha256_hex"]}`.

What it proves: only `tool.run` ran in the pool. The `tool_call` events, spans,
guardrails and approvals were prepared serially in declared order, and the
`tool_result` events and messages finish in declared order too — so `seq` stays
contiguous and an approval can never race another tool. The same manifest with
`parallel_tools: false` records `{"parallel": false}`.

The flag is the whole API change: one boolean in YAML, no new protocol.

## 3. Service auth — a token, or loopback only (ADR-0021)

Run step 1 or 2 first: `harness serve` needs an event log to read.

```bash
RIGGING_SERVICE_TOKEN=demo-token make serve &
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/runs
# 401
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Authorization: Bearer demo-token' http://127.0.0.1:8000/api/runs
# 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/
# 200 — the page is public static markup and prompts for the token
uv run harness serve --host 0.0.0.0
# exits 2: no token configured, so a non-loopback bind is refused
```

What it proves: every `/api/*` route is guarded by `hmac.compare_digest` against
one or more comma-separated tokens (rotation works); `GET /` stays public because
it carries no run data; the stream uses `fetch` + `ReadableStream` so the token
travels in a header and never in a URL. The read-only guarantee is unchanged:
SQLite `mode=ro`, no launch or approve endpoint.

In the browser, open <http://127.0.0.1:8000>, paste `demo-token`, and watch a
run replay then follow live.

## 4. MLflow — one rigging run, one MLflow run (ADR-0020)

```bash
uvx mlflow server --backend-store-uri sqlite:///mlflow.db --port 5000 &
uv run harness run examples/mlflow/agent.yaml "say hello in one sentence"
curl -s 'http://localhost:5000/api/2.0/mlflow/runs/search' \
  -H 'content-type: application/json' \
  -d '{"experiment_ids":["0"],"max_results":5}' | python -m json.tool
```

Expected: one MLflow run named `rigging`, status `FINISHED`, with tags
`rigging.run_id` / `gen_ai.agent.name` / `gen_ai.request.model` and metrics for
tokens, steps, latency, cost and chat/tool call counts.

What it proves: a second `Exporter` with no SDK and no new dependency — four
calls over the `2.0` tracking REST API. A denied or budget-exhausted run shows
`FAILED` rather than stuck `RUNNING`, because the loop now ends the root span on
every exit path (the loop fix that ADR-0020 forced). Point it at a hosted server
with `MLFLOW_TRACKING_URI` / `MLFLOW_TRACKING_TOKEN`.

## One-screen version

```bash
make check
uv run harness run examples/subagents/agent.yaml "Use the researcher to explain a vector database in one sentence." --yes && uv run harness runs
uv run harness run examples/parallel/agent.yaml "Call current_time for Europe/Zurich and sha256_hex for 'rigging' in one response."
RIGGING_SERVICE_TOKEN=demo-token make serve
```

If you only have sixty seconds, run the first line and the subagent demo: the
indented child run is the most legible difference between v0.5 and v0.6.
