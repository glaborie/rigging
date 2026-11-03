# rigging

> Capabilities are declared in a manifest and discovered through a registry.
> The loop, the gateway, the guardrails, the observability backend and the
> retriever are all swappable implementations of small protocols.

**Status: v0.6.1 released (2026-11-03).** The harness includes a registry-driven
agent loop, event log, MCP tools, OTel/Langfuse and MLflow tracing, an eval CLI,
three pluggable decision-model adapters, automatic entry-points discovery,
subagents as child runs, opt-in parallel tool execution, and a read-only web view
over the same event log with bearer-token auth. See
[`docs/milestones.md`](docs/milestones.md) for milestone evidence and
[`docs/showcase-v0.6.md`](docs/showcase-v0.6.md) for a four-command demo.

![the demo: tools, a guarded refund run, and the event trail it wrote](docs/demo.gif)

Recorded from [`scripts/demo.sh`](scripts/demo.sh) by `make record` — the GIF is
rendered from [`docs/demo.cast`](docs/demo.cast), which replays with
`asciinema play docs/demo.cast`. The run is real: host Ollama, the seeded demo
database, and a scratch event log.

---

## Why

Most agent frameworks pick a layer to own — the loop, or the graph, or the
prompts — and then hide the rest. `rigging` picks nothing. It defines a small
set of protocols (`LLMClient`, `Tool`, `Guardrail`, `Retriever`,
`DecisionModel`, `Exporter`) and a registry that lets implementations be
assembled per agent, from a single API call in a script up to a full harness
with skills, guardrails, approval gates, escalation, retrieval and tracing.

Swapping the gateway is a base URL. Swapping the guardrail set is four names in
a YAML file. Swapping Qdrant for anything else is one class.

## Capability matrix

| Capability | Registry source | v0.1 | v0.2 | v0.3 | v0.4 | v0.5 | v0.6 |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Agent loop + streaming | core | ● | | | | | |
| Tools (JSON-schema function calling) | decorator | ● | | | | | |
| Registry + entry-point discovery (on by default) | core | ● | | | ● | ● | |
| Skills (progressive disclosure) | filesystem | | ● | | | | |
| SQLite event log + sessions | core | | ● | | | | |
| `harness chat` REPL | core | | ● | | | | |
| Guardrails (4 built-ins) | decorator | | | ● | | | |
| Approval policy (side-effecting tools) | decorator | | | ● | | | |
| Escalation to human | tool | | | ● | | | |
| Retrieval (Qdrant default, FTS5 reference) | decorator | | | ● | | | |
| Laya decision model (advisory) | decorator | | | ● | | | |
| OTel → Langfuse exporters | core | | | | ● | | |
| MLflow exporter (one run per run, no SDK) | core | | | | | | ● |
| `harness eval` | core | | | | ● | | |
| MCP servers as a tool source | entry point | | | | ● | | |
| Manifest JSON Schema + docs | -- | | | | | ● | |
| Web widget (read-only, SSE) | extra | | | | | ● | |
| Subagents (`SubagentRunner`) | manifest + protocol | | | | | | ● |
| FastAPI service + auth (bearer) | extra | | | | | | ● |
| Parallel tool execution (opt-in) | manifest flag | | | | | | ● |

`●` shipped · `◐` stretch goal

## Quickstart

```bash
# 1. Model access — pick one
ollama signin                              # beam:cloud, free at time of writing
# or the fully local path:
ollama pull qwen3:8b

# 2. Install (dev + service extras)
make install

# 3. Run an agent (the no-tools smoke agent)
uv run harness run examples/hello/agent.yaml "Say hello in one sentence." --model qwen3:8b

# Interactive chat: one session, many runs, continuous memory
uv run harness chat examples/hello/agent.yaml

# Offline: replay a recorded transcript instead of calling the API
uv run harness run examples/hello/agent.yaml "hi" --replay examples/hello/transcript-codestral.jsonl

# The canonical demo: 5 tools, 2 skills, FTS5 retrieval, 3 guardrails, approval
make seed                                        # deterministic demo.db (seed=7)
make demo MODEL=qwen3:8b PROMPT="What is the refund policy for ORD-1002?"
make eval MODEL=qwen3:8b                          # event-log predicates, per-case table
```

No API key is required for either path. The gateway is OpenAI-compatible HTTP,
so pointing `RIGGING_BASE_URL` at OmniRoute, vLLM, LM Studio or Ollama's own
`/v1` is all it takes to switch providers.

## Web view

A read-only page over the same SQLite event log — no second store, no write
endpoint, no build step ([ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md)).

```bash
make hello                      # or make demo — anything that writes rigging.db
make serve                      # http://127.0.0.1:8000
```

The left column lists runs; picking one replays its events and then follows it
live over SSE (`/api/runs/{id}/stream`). Approvals stay in the loop: the page
cannot launch a run or answer a guardrail, by design.

Auth is one bearer token (ADR-0021). Set `RIGGING_SERVICE_TOKEN` (comma-separated
for rotation) and every `/api/*` route requires it; `GET /` stays public so the
page can ask for the token, which it keeps in `sessionStorage` and sends in a
header — never in a URL. With no token configured the server refuses any
non-loopback bind.

```bash
RIGGING_SERVICE_TOKEN=demo-token make serve
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/runs   # 401
curl -s -H 'Authorization: Bearer demo-token' http://127.0.0.1:8000/api/runs
```

The page's JavaScript is tested in a real browser, not only over HTTP:

```bash
make browser-install            # playwright + Chromium (~120 MB, once)
make test-browser               # loads the page, asserts the run list renders
```

That test exists because v0.6.0 shipped a page that answered 200 and rendered
nothing: a Python `\n` had become a real newline inside the inline JS, so the
script never parsed. The route tests could not see it; the browser test can. The
postscript in [ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md)
has the honest version.

## Decision models: cloud or local

Decision models share the `DecisionModel` protocol and registry; select one by
its literal model id. Ollama's SystemOne API runs locally, with no API key,
cloud quota, or extra Python dependency. Requires Ollama 0.35 or later.

```bash
ollama pull d1-omni                 # local 600M model (~850 MB)
uv run harness tools --kind decision_model
uv run harness calibrate d1-omni --dataset tests/data/decisions.jsonl
uv run harness calibrate respan/span-01-lite:free --dataset tests/data/decisions.jsonl
```

Both scored models — one cloud, one local — ran the **same 42-case** labelled
dataset (`tests/data/decisions.jsonl`; 22 `no`, 20 `yes`). Accuracy is the argmax
choice against the label; Brier is the multiclass Brier score, where lower is
better.

| model | where | n | accuracy | Brier |
|---|---|--:|--:|--:|
| `respan/span-01-lite:free` | OpenRouter (cloud) | 42 | **0.833** | **0.269** |
| `d1-omni` | Ollama (local) | 42 | 0.548 | 0.479 |
| `@cf/cloudflare/clef-flash` | FreeLLMAPI | — | not run | — |

That is an advisory signal, not a safety gate; deterministic guardrails remain
authoritative (ADR-0009). The cloud model scores better; the local model is the
zero-key fallback. `@cf/cloudflare/clef-flash` passes its contract tests but its
live scoring run hit the daily free quota (HTTP 429), so it has no numbers yet.
The local endpoint defaults to `http://localhost:11434`; set `OLLAMA_BASE_URL` to
override it.

## Repository layout

```
rigging/            the package (protocols, registry, loop, client, CLI)
  protocols.py      every swappable interface, one screen each
  types.py          canonical message/tool/reasoning types
  registry.py       decorator registry + entry-point discovery
  events.py         event-sourced run log (SQLite)
  loop.py           the agent loop (v0.1)
  client/           OpenAI-compatible HTTP client + SSE parser
  guardrails/       four deterministic built-ins
  retriever/        Qdrant + FTS5 implementations of Retriever
  decide/           Laya decision model adapter
  service/          read-only FastAPI + SSE view of the event log (ADR-0018)
  subagents.py      spawn_<name> tools that run a child agent (ADR-0019)
docs/
  architecture.md   how the layers fit and why
  milestones.md     v0.1 → v0.6 with the go-public gate
  adr/              one page per decision, numbered
rigging/schema/
  agent.schema.json machine-checkable manifest contract
examples/b2b-support/   the canonical demo agent
examples/subagents/     parent + child manifests (v0.6 delegation)
examples/parallel/      two tools in one response (v0.6 parallel_tools)
examples/mlflow/        the MLflow exporter smoke agent (v0.6)
scripts/          demo recording, Langfuse tail, gateway password reset
tests/            unit + SSE wire fixtures + replay integration
```

## Documentation

- [`AGENTS.md`](AGENTS.md) — operating instructions for a coding agent working in this repo

- [`docs/architecture.md`](docs/architecture.md) — layers, loop sequence, event schema
- [`docs/adr/`](docs/adr/) — 22 decisions with the alternatives that were rejected
- [`docs/milestones.md`](docs/milestones.md) — what ships when, and the publish gate
- [`docs/showcase-v0.6.md`](docs/showcase-v0.6.md) — four commands that demonstrate v0.6

## Manifest schema

Every `agent.yaml` manifest is validated against the JSON Schema at
`rigging/schema/agent.schema.json`. Print it with:

```bash
uv run harness schema
```

The schema ensures manifests have valid structure and documents all
configurable options. Use it with editor tooling for autocomplete and
validation.

## Third-party capabilities

Third-party packages can register tools, guardrails, retrievers, and decision
models via Python entry points. See
[`examples/third-party-tools/README.md`](examples/third-party-tools/README.md)
for the registration pattern. Entry-points are discovered automatically when
`assemble()` is called without an explicit `Registry`.

## Non-goals

Deliberately not built (see [ADR-0017](docs/adr/0017-deliberate-non-goals.md)):
agent long-term memory, serving MCP, LLM-as-judge guardrails or evals, a
*write-capable* web UI, model fine-tuning, Windows-native support, and a plugin
marketplace beyond entry points.

Four items were on that list and have since shipped, each with its own ADR:
the read-only web view (v0.5, [ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md)),
subagents (v0.6, [ADR-0019](docs/adr/0019-subagents-are-child-runs.md)),
service auth (v0.6, [ADR-0021](docs/adr/0021-service-auth-is-a-bearer-token.md)),
and opt-in parallel tool execution (v0.6, [ADR-0022](docs/adr/0022-parallel-tool-execution-is-opt-in.md)).
ADR-0017 records the supersessions; it is not silently edited away.


## Development TODO

See [TODO.md](TODO.md) for ongoing skills development work including:
- MLOps feature store parity debugging
- Eval-driven routing architecture design  
- ML pipeline CD pipeline implementation
- Verification milestones for production readiness

## License

MIT. See [LICENSE](LICENSE).
