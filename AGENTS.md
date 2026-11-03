# AGENTS.md

Operating instructions for any coding agent working in this repository. Read this
file, then `docs/architecture.md`, then the relevant ADR **before** writing code.

## What this is

`rigging` is a modular agent harness. A YAML manifest names capabilities; a
registry resolves them; every layer (gateway client, tools, guardrails, retriever,
decision model, exporters) is a small protocol with swappable implementations.
The project is a portfolio piece: **the code, the ADRs and the demo are the
deliverable, and a reviewer's first five minutes matter as much as the tests.**

Current state: v0.6.3 released (2026-11-03). The loop,
client, registry, event log, manifest, guardrails, retriever, Laya decision model,
MCP tools, OTel/Langfuse + MLflow exporters, eval CLI, read-only web view with
bearer auth, subagents and opt-in parallel tool execution are implemented and
tested (185 offline tests plus a browser smoke test; ruff + `mypy --strict`
clean). All four exit-criteria demos verified live.

## Hard rules (violating these fails review)

1. **The core never imports a vendor.** `rigging/protocols.py` imports only
   `rigging.types`. No `openai`, no `litellm`, no `anthropic`, no Langfuse SDK in
   the loop. Gateways are spoken to as OpenAI-compatible HTTP.
2. **Every step is an event.** Anything the loop does that a debugger or an eval
   might want must be appended to `run_events`. If it is not in the log, it did
   not happen.
3. **A tool can never crash a run.** Exceptions, bad JSON args, unknown tools,
   timeouts and denials all become structured `tool` messages with `is_error`.
4. **A side-effecting tool can never run unapproved.** Enforcement lives in the
   loop via `ApprovalPolicy`; never rely on a tool's docstring or good behaviour.
   Default policy is `auto_deny`.
5. **The event log is a security boundary.** All payloads pass `redact_event()`
   before insert. Never write a payload around it, never log a secret "just for
   debugging". `tests/test_redact.py` asserts on the database file.
6. **No new abstraction without an ADR.** New protocol, new layer, new hook →
   a numbered page in `docs/adr/` first (context, decision, consequences,
   alternatives rejected). One page, honest about costs.
7. **No new dependency without justification.** Core deps are `httpx`,
   `pydantic`, `pydantic-settings`, `pyyaml`. Everything heavier goes behind an
   extra (`[qdrant]`, `[laya]`, `[langfuse]`, `[mcp]`, `[service]`, `[browser]`).
   The widget is
   `[service]`, and `make install` pulls it so `make check` exercises it.
8. **No LLM-as-judge** in guardrails or evals (ADR-0009, ADR-0017).
9. **No silent feature: the CLI says what is not built.** Unimplemented commands
   print the milestone and exit 2.
10. **Non-goals stay non-goals** (ADR-0017). Agent memory, MCP serving and
    write-capable UIs are still out. Subagents (v0.6, ADR-0019) and a read-only
    web view (v0.5, ADR-0018) are in: extend those, do not reinvent them.

## Layout

```
rigging/
  types.py          canonical Message/ToolCall/Usage/ModelResponse (+ reasoning)
  protocols.py      every swappable interface; imports only types
  registry.py       decorators + entry-point discovery; resolve_all fails fast
  loop.py           the agent loop; invariants 2-4 live here
  events.py         SQLite append-only log, projections, JSONL export
  manifest.py       YAML load, JSON Schema validation, single-level defaults
  redact.py         the security boundary
  client/           OpenAI-compatible httpx client + hand-written SSE parser
  guardrails/       pii, injection, consequential_action, commitment
  retriever/        fts5 (works), qdrant (v0.3 stub)
  decide/           laya (v0.3 stub, advisory only)
  tools/            function -> Tool adapter, JSON Schema from annotations
  service/          read-only FastAPI + SSE view of the event log (ADR-0018)
  subagents.py      spawn_<name> tools that run a child agent (ADR-0019)
  cli.py            harness run|schema|tools|chat|index|eval|calibrate|serve
rigging/schema/agent.schema.json   the manifest contract (authoritative)
docs/architecture.md               layers, loop sequence, event schema
docs/adr/                          one page per decision, numbered
docs/milestones.md                 the task list; definitions of done
docs/threat-model.md               assets, threats, controls, non-claims
examples/b2b-support/              the canonical demo (manifest + skills final)
examples/subagents/                parent + child manifests (v0.6)
examples/parallel/                 two tools in one response (v0.6)
examples/mlflow/                   MLflow exporter smoke agent (v0.6)
docs/showcase-v0.6.md              four commands, four ADRs — the v0.6 demo
BENCHMARKING.md                    model-id syntax, profiles, reading the table
scripts/bench.py                   models x repeats driver -> results/
scripts/langfuse_to_atif.py        Langfuse session -> ATIF v1.7 trajectory file
results/                           published table + raw runs (evidence, not code)
tests/                             unit, SSE wire fixtures, loop invariants,
                                   and the opt-in browser smoke test
```

## Workflow

```bash
uv sync --extra dev          # env
make help                    # every target, every override, gateway choices
make check                   # ruff + mypy --strict + pytest (no network)
make test-browser            # opt-in: render the web view in headless Chromium
uv run harness tools         # registry sanity
uv run harness run examples/hello/agent.yaml "hello"   # needs a gateway (see below)
```

- Work in the Unix environment (WSL/Linux). **Line endings must be LF** — a CRLF
  file breaks `make` and shell scripts.
- Do not touch `.venv/`, `*.db`, `runs/`. They are gitignored; never commit them.
- Tests must not touch the network. Real-model runs are marked `-m live` and are
  opt-in. New behaviour needs a test in the same commit — a guardrail without a
  test asserting its verdict is not done.
- Commit messages: `area: imperative summary` (e.g. `loop: ask for a final answer
  at budget exhaustion`). One logical change per commit; the history is part of
  the portfolio.

## Gateway / models

The harness is gateway-agnostic; `RIGGING_BASE_URL` decides. Defaults assume an
OpenAI-compatible endpoint on `:20128` (OmniRoute, `make gateway`) or Ollama's own
`/v1` on `:11434`. Default model is `beam:cloud` (free at time of writing, needs
`ollama signin`); `qwen3:8b` is the fully local fallback.

### Quickstart options

```bash
# Option 1: replay (offline, no gateway needed)
uv run harness run examples/hello/agent.yaml "hello" --replay examples/hello/transcript-codestral.jsonl

# Option 2: OpenRouter (requires RIGGING_API_KEY)
RIGGING_BASE_URL="https://openrouter.ai/api/v1" RIGGING_API_KEY=<your-key> \
  uv run harness run examples/hello/agent.yaml "hello" --model "z-ai/glm-5.3-flashx-legal"

# Option 3: OmniRoute with profile (requires API key)
RIGGING_API_KEY=<omniroute-key> uv run harness run examples/hello/agent.yaml "hello" --model "auto/chat"
```

### Observability (Langfuse Cloud)

There is **no local Langfuse stack** — `docker compose` runs only `omniroute`
and, under `--profile retrieval`, `qdrant`. Traces go to Langfuse Cloud:

```bash
make observability              # checks LANGFUSE_* and the [langfuse] extra — emits nothing
make trace                      # runs the observability example, then reads the spans back
make trace MODEL=qwen3:8b       # ...against host Ollama; no gateway container needed
make eval EXPORT=langfuse       # the b2b eval suite, one trace per case
make eval PROFILE=default MODEL=openrouter/...   # hosted model via OmniRoute, not Ollama
```

`PROFILE=default` (or `harness eval --profile default`) overrides the manifest's
`credential_profile`: the demo pins Ollama for zero-credential runs, so a hosted
model id would otherwise be sent to `localhost:11434` and 404.

The exporter needs the `[langfuse]` extra, which `make install` does not pull:
`uv sync --extra dev --extra langfuse` (add `--extra service --extra mcp` if you
use the widget or MCP). Without it the loop silently skips spans — the run
succeeds and nothing arrives.

`.env` needs `LANGFUSE_HOST` (defaults to `https://cloud.langfuse.com`),
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`. The keys are read through
`Settings`, so `harness run` picks them up from `.env` — do not `source` the file
first. Named credential profiles work the same way: `extra="allow"` keeps
`RIGGING_PROFILE_<NAME>_BASE_URL/_API_KEY` from `.env` (real environment wins).
The exporter resolves the host as
`endpoint → env LANGFUSE_HOST → .env LANGFUSE_HOST → env LANGFUSE_BASE_URL → cloud
default`; the OTLP path `/api/public/otel/v1/traces` is appended once. Exporters
are closed on **every** exit path (ok, denied, budget_exhausted, output-deny) —
a batch processor that is never flushed drops its spans.

Neither `make check` nor `make observability` emits a span: the tests are offline
by rule and the credential check runs no agent. `make trace` is the one command
that proves the whole path — it writes a timestamp marker, runs the example, then
polls `GET /api/public/v2/observations` until the spans show up (ingestion lags a
few seconds; `GET /api/public/traces` is removed 2026-11-16). A self-hosted
Langfuse v3 needs Postgres + ClickHouse + Redis and is deliberately not in the
compose file.

## Definition of done

A task is done when: the behaviour exists behind the documented protocol; there is
a test that fails without it (and an event type if it changes run state); `make
check` is green; the affected doc or ADR says what changed; and no user-facing
text claims something the code does not do.

## Where to start

`docs/milestones.md` → the first unticked box. v0.1 is **done** (2026-10-29: live
end-to-end runs through OmniRoute -> freellmapi -> codestral and beam:cloud, two-tool
task complete, 30 tests green). v0.2 shipped (tag `v0.2.0`, release published). v0.3 shipped 2026-10-29:
guardrails + approval live in the demo, fts5/qdrant retrievers, seeded fixture DB,
`harness index`. All four exit-criteria demos verified live. v0.4 shipped 2026-10-29:
OTel → Langfuse observability, eval CLI over recorded transcripts (`--replay`),
MCP consumption. v0.5 shipped the web view and the recorded demo; v0.6
(2026-11-03) added subagents, the MLflow exporter, service auth and opt-in
parallel tool execution. Start from the first unticked box above that.

## Things that will tempt you and are wrong here

- Adding `openai` because the wire format is tedious → ADR-0001; the SSE parser
  exists on purpose and has fixtures.
- Storing messages in their own table "for convenience" → ADR-0005; the event log
  is the truth, projections are queries.
- Adding `max_steps`-style `try/except` guards that end the run silently → ADR-0010;
  on exhaustion ask for a final answer, then return `BudgetExhausted` with the
  partial transcript.
- Adding `tiktoken` to pre-count tokens → ADR-0011; gateway `usage` is
  authoritative, unknown models skip the USD limit.
- A "knowledge:" block that auto-injects documents → ADR-0008; retrieval is a tool.
- Making Laya the hard gate → ADR-0009; it is advisory until
  `tests/data/decisions.jsonl` shows its calibration.
- Writing a web UI before v0.5 → ADR-0017. The REPL comes first.
- Treating a 200 from `/` as proof the page works → ADR-0018 postscript; only
  `make test-browser` parses and runs the inline JS.
