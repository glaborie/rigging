# rigging

> Build your own agent harness. You choose the loop, gateway, guardrails, retriever and tracing backend. 
> The kit gives you the protocols, the registry, a replayable event log, approval gates and an eval harness.

**Status: v0.7.0 released (2026-11-03).** The harness includes a registry-driven
agent loop, event log, MCP tools, OTel/Langfuse and MLflow tracing, an eval CLI,
three pluggable decision-model adapters, automatic entry-points discovery,
subagents as child runs, opt-in parallel tool execution, and a web view over the
same event log with bearer-token auth — read-only, or chat with in-page approvals.
See [`docs/milestones.md`](docs/milestones.md) for milestone evidence and
[`docs/showcase-v0.7.md`](docs/showcase-v0.7.md) for the browser demo.

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

| Capability | Registry source | v0.1 | v0.2 | v0.3 | v0.4 | v0.5 | v0.6 | v0.7 |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Agent loop + streaming | core | ● | | | | | | |
| Tools (JSON-schema function calling) | decorator | ● | | | | | | |
| Registry + entry-point discovery (on by default) | core | ● | | | ● | ● | | |
| Skills (progressive disclosure) | filesystem | | ● | | | | | |
| SQLite event log + sessions | core | | ● | | | | | |
| `harness chat` REPL | core | | ● | | | | | |
| Guardrails (4 built-ins) | decorator | | | ● | | | | |
| Approval policy (side-effecting tools) | decorator | | | ● | | | | |
| Escalation to human | tool | | | ● | | | | |
| Retrieval (Qdrant default, FTS5 reference) | decorator | | | ● | | | | |
| Laya decision model (advisory) | decorator | | | ● | | | | |
| OTel → Langfuse exporters | core | | | | ● | | | |
| MLflow exporter (one run per run, no SDK) | core | | | | | | ● | |
| `harness eval` | core | | | | ● | | | |
| MCP servers as a tool source | entry point | | | | ● | | | |
| Manifest JSON Schema + docs | -- | | | | | ● | | |
| Web widget (read-only, SSE) | extra | | | | | ● | | |
| Subagents (`spawn_<name>` tools) | manifest + child manifest | | | | | | ● | |
| FastAPI service + auth (bearer) | extra | | | | | | ● | |
| Parallel tool execution (opt-in) | manifest flag | | | | | | ● | |
| Chat + in-page approvals (opt-in) | extra | | | | | | | ● |

`●` shipped · `◐` stretch goal

## Quickstart

```bash
# 1. Model access — the canonical demo needs an OpenRouter key
#    RIGGING_PROFILE_OPENROUTER_API_KEY=sk-or-...   (in .env)
# Fully local alternative for the smoke agent:
ollama pull qwen3:8b

# 2. Install (dev + service extras)
make install

# 3. Build your own manifest: an interview, then a validated agent.yaml
uv run harness init                          # writes agents/<name>/agent.yaml + prompt.md
uv run harness init --yes --dir /tmp/agents  # or take every suggested default

# 4. Run an agent (the no-tools smoke agent)
uv run harness run examples/hello/agent.yaml "Say hello in one sentence." --model qwen3:8b

# Interactive chat: one session, many runs, continuous memory
uv run harness chat examples/hello/agent.yaml

# Handoffs waiting for a human (escalation events, newest first)
uv run harness escalations

# Offline: replay a recorded transcript instead of calling the API
uv run harness run examples/hello/agent.yaml "hi" --replay examples/hello/transcript-codestral.jsonl

# The canonical demo: 12 tools, 2 skills, FTS5 retrieval, 5 guardrail hooks, approval
make seed                                        # deterministic demo.db (seed=7)
make demo PROMPT="What is the refund policy for ORD-1002?"   # deepseek via OpenRouter
make demo MODEL=qwen3:8b PROFILE=ollama PROMPT="What is the refund policy for ORD-1002?"
make eval                                        # event-log predicates, per-case table
make eval EXPORT=langfuse                        # ...and one Langfuse trace per case
make demo EXPORT=langfuse PROMPT="What is the refund policy for ORD-1002?"
make eval MODEL=qwen3:8b PROFILE=ollama          # the fully local path

# Drive the same agent from a browser: type, watch the tools run, approve a refund
RIGGING_SERVICE_TOKEN=demo make web              # add EXPORT=langfuse for one trace per turn
# http://127.0.0.1:8000 — token "demo"
```

`MODEL=`, `PROFILE=` and `EXPORT=` override the manifest's model,
`credential_profile` and exporters for `run`, `chat`, `eval` and `serve`/`web`
VERBOSE=1 make web                               # ...and one stdout line per HTTP request
(`harness run|chat|serve --export langfuse` is the same switch). Traces go to
Langfuse Cloud using the `LANGFUSE_*` keys in `.env`; `make trace` runs an agent
and prints the spans it produced. The demo pins `deepseek/deepseek-v4.1-flash`
through the `openrouter` profile; `PROFILE=ollama` and a model Ollama serves
(`qwen3:8b`) is the offline path.

For a multi-model, multi-repeat table — and the model-id syntax that goes with
it — see [`BENCHMARKING.md`](BENCHMARKING.md).

The gateway is OpenAI-compatible HTTP, so pointing `RIGGING_BASE_URL` at
OmniRoute, vLLM, LM Studio or Ollama's own `/v1` is all it takes to switch
providers; a named profile in `.env` keeps the key next to the base URL.

## Trajectories (ATIF)

A Langfuse session becomes an [ATIF v1.7](https://docs.nvidia.com/nemo/relay/latest/configure-plugins/observability/atif.md)
trajectory file, which external viewers can open:

```bash
make atif SESSION=s_a602a7c7b060                 # one file per run (turn)
make atif SESSION=s_a602a7c7b060 COMBINE=1       # one file for the whole conversation
```

Then drop the file into the OpenHands trajectory visualizer at
<http://localhost:12000>. One trace is one *run* — one prompt, its model calls
and tool calls. A session is several runs, so `COMBINE=1` concatenates them into
a single trajectory: steps and tool-call ids are renumbered, each step keeps the
`trace_id` it came from, and `extra.rigging.runs` lists the runs with their eval
verdicts. Without it, a six-turn session writes six files. The conversion is a
script, not an exporter — `scripts/langfuse_to_atif.py --help`.

The `harbor` CLI wants a job directory rather than a bare file, so `HARBOR=`
writes that layout too:

```bash
make atif SESSION=s_a602a7c7b060 HARBOR=runs/harbor
harbor view runs/harbor --port 8080
# http://127.0.0.1:8080/jobs/rigging/trials/s_a602a7c7b060
```

It lists trials from `<trial>/result.json` and reads
`<trial>/agent/trajectory.json`, so the ATIF file lands there. Repeat sessions
accumulate in the same job.

A trial exported from an eval run also carries the eval's verdict: `harness eval`
writes an `eval_verdict` event, the exporter joins it by the run id in the trace,
and the Harbor trial gets `verifier_result.rewards = {"reward": 1.0|0.0}`. That
is what fills Harbor's Reward column and Outcomes view — a projection of the
suite's assertions, not a score invented for the viewer.

## Web view

The same SQLite event log, rendered in a browser: no second store, no build step.
Two modes, one page ([ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md),
[ADR-0023](docs/adr/0023-the-web-ui-can-run-an-agent.md)).

```bash
make hello                      # or make demo — anything that writes rigging.db
make serve                      # read-only: http://127.0.0.1:8000

# Chat: type a prompt, watch the tools run, approve or deny a refund
RIGGING_SERVICE_TOKEN=demo make web MODEL=opencode-go/deepseek-v4.1-flash PROFILE=default
```

The left column lists runs; picking one shows the session's earlier turns and then
follows the selected run live over SSE (`/api/runs/{id}/stream`). The transcript
is built from events — user and agent bubbles, one card per tool call with its
arguments and result, a guardrail line per hook, and a footer with the model
turns and token counts. Clicking anywhere on a tool card folds or unfolds its
JSON (except the approve/deny buttons and a text selection you are dragging),
and the folded state survives the live re-renders. `raw events` switches to the
unfiltered log. Agent answers render as markdown, including tables.

The **escalations** tab in that column is the human queue: reason, session,
summary and fact keys for each handoff (`GET /api/escalations`), and clicking a
row opens the run, where the transcript shows the escalation note and its facts.

**Read-only is the default.** Without `--manifest` there is no write route at
all: no POST, no launch, no approval. That is still the whole of ADR-0018.

**With `--manifest` the page can start a run.** `POST /api/chat` calls the same
`run_agent` the CLI calls, in a worker thread; the page subscribes to the run's
stream. Multi-turn history is rebuilt from the log (`session_messages`), so a
restart loses nothing. A side-effecting tool blocks the loop on `WebApproval`
until the page answers — the loop writes `approval_requested` and
`approval_verdict` exactly as it does in the terminal, and still makes the
decision. There is no second loop, no second store, and no auto-approve.

Chat requires a token even on loopback: any page the browser visits can POST to
`127.0.0.1:8000`, and the token is the one thing it cannot read. `harness serve`
refuses `--manifest` without `RIGGING_SERVICE_TOKEN`.

Auth is one bearer token (ADR-0021). Set `RIGGING_SERVICE_TOKEN` (comma-separated
for rotation) and every `/api/*` route requires it; `GET /` stays public so the
page can ask for the token, which it keeps in `sessionStorage` and sends in a
header — never in a URL. With no token configured the server refuses any
non-loopback bind.

`/api/*` is also rate-limited per client IP: 240 requests/minute by default
(`RIGGING_SERVICE_RATE_LIMIT`, `0` disables). The check runs before the token,
so a script guessing tokens gets `429` and a `Retry-After` rather than unlimited
attempts. Behind a proxy, `X-Forwarded-For` sets the key only when the immediate
peer is in `RIGGING_SERVICE_TRUSTED_PROXIES` (loopback by default); `*` is
refused and an untrusted peer's header is ignored. It is a backstop; the proxy
should rate-limit too.

**Exposing it needs TLS.** A bearer token over plain HTTP crosses the network in
clear text, so `harness serve` refuses a non-loopback bind with a token unless
`--ssl-certfile/--ssl-keyfile` are given, or `RIGGING_SERVICE_ALLOW_PLAINTEXT=1`
says the network already protects the link. The supported setup is TLS at a
reverse proxy with uvicorn on loopback:

```bash
RIGGING_SERVICE_TOKEN="$(openssl rand -base64 32)" \
  uv run harness serve --host 127.0.0.1 --port 8000
# Caddy, with automatic certificates:
#   view.example.com { reverse_proxy 127.0.0.1:8000 }
```

`sessionStorage` is readable by any script running on the origin. That is
accepted here because the page loads no third-party code, escapes every payload
and, in the default mode, can only read; a per-response CSP nonce blocks external
script/style and framing. In chat mode the token also guards a write path, and
the same XSS would let a script start a run — the CSP and the absence of
third-party script are the controls, stated as residual risk rather than hidden.
[`docs/threat-model.md`](docs/threat-model.md) has the threat table, controls,
deployment checklist and the non-claims (no encryption at rest, no per-user
audit, no token expiry).

```bash
RIGGING_SERVICE_TOKEN=demo-token make serve
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/runs   # 401
curl -s -H 'Authorization: Bearer demo-token' http://127.0.0.1:8000/api/runs
```

The page's JavaScript is tested in a real browser, not only over HTTP:

```bash
make browser-install            # playwright + Chromium (~120 MB, once)
make test-browser               # renders the run list; runs a turn and approves it
```

That test exists because v0.6.0 shipped a page that answered 200 and rendered
nothing: a Python `\n` had become a real newline inside the inline JS, so the
script never parsed. The route tests could not see it; the browser test can. The
second test drives the write path end to end — types a prompt, clicks approve,
and asserts the tool result appears. The postscript in
[ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md)
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
  init_agent.py     `harness init`: the manifest interview (no new dependency)
docs/
  architecture.md   how the layers fit and why
  milestones.md     v0.1 → v0.6 with the go-public gate
  threat-model.md   assets, threats, controls and the deployment baseline
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
- [`docs/adr/`](docs/adr/) — 23 decisions with the alternatives that were rejected
- [`docs/milestones.md`](docs/milestones.md) — what ships when, and the publish gate
- [`docs/threat-model.md`](docs/threat-model.md) — assets, threats, controls, non-claims
- [`docs/showcase-v0.7.md`](docs/showcase-v0.7.md) — drive the agent from a browser, approve a refund
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

### Creating one

`harness init` is a configure-style interview: every question lists the real
choices from the registry, carries a suggested default, and Enter takes it.

```bash
uv run harness init                      # -> agents/<name>/agent.yaml + prompt.md
uv run harness init --dir /tmp/agents --yes   # unattended: every default
```

- **The choices are read, not hardcoded.** Tools, guardrails and retrievers come
  from the registry, so the menus cannot drift from the code. Selected tools that
  declare `side_effecting` are written into `approval.side_effecting_tools`
  automatically, which is the one field a hand-written manifest forgets.
- **The file is validated before it is written.** The answers are dumped to a
  temp file and loaded through `load_manifest`, so `harness init` cannot emit a
  manifest `harness run` would reject. Nothing is written if validation fails.
- **It never overwrites silently.** An existing `agent.yaml`/`prompt.md` is an
  error unless you pass `--force`.
- **The prompt lives in `prompt.md`,** referenced by `system_prompt_file`, so the
  YAML stays readable in a diff. The generated file's first lines carry a
  `yaml-language-server: $schema=` comment — a comment because the contract sets
  `additionalProperties: false` and would reject a real `$schema` key.

It generates the core plus safety block (model, profile, prompt, tools,
retriever, guardrails per hook, approval, limits, exporters). Skills, `mcp_servers`,
`subagents` and `parallel_tools` are still hand-written — `examples/` covers each.

## Third-party capabilities

Third-party packages can register tools, guardrails, retrievers, and decision
models via Python entry points. See
[`examples/third-party-tools/README.md`](examples/third-party-tools/README.md)
for the registration pattern. Entry-points are discovered automatically when
`assemble()` is called without an explicit `Registry`.

## Non-goals

Deliberately not built (see [ADR-0017](docs/adr/0017-deliberate-non-goals.md)):
agent long-term memory, serving MCP, LLM-as-judge guardrails or evals, model
fine-tuning, Windows-native support, and a plugin marketplace beyond entry
points.

Five items were on that list and have since shipped, each with its own ADR:
the read-only web view (v0.5, [ADR-0018](docs/adr/0018-web-widget-is-a-read-only-view-of-the-event-log.md)),
subagents (v0.6, [ADR-0019](docs/adr/0019-subagents-are-child-runs.md)),
service auth (v0.6, [ADR-0021](docs/adr/0021-service-auth-is-a-bearer-token.md)),
opt-in parallel tool execution (v0.6, [ADR-0022](docs/adr/0022-parallel-tool-execution-is-opt-in.md)),
and the *write-capable* web UI (v0.7, [ADR-0023](docs/adr/0023-the-web-ui-can-run-an-agent.md)) —
opt-in behind `--manifest`, and still run by the loop. ADR-0017 records the
supersessions; it is not silently edited away.

## License

MIT. See [LICENSE](LICENSE).
