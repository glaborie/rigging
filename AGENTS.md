# AGENTS.md

Operating instructions for any coding agent working in this repository. Read this
file, then `docs/architecture.md`, then the relevant ADR **before** writing code.

## What this is

`rigging` is a modular agent harness. A YAML manifest names capabilities; a
registry resolves them; every layer (gateway client, tools, guardrails, retriever,
decision model, exporters) is a small protocol with swappable implementations.
The project is a portfolio piece: **the code, the ADRs and the demo are the
deliverable, and a reviewer's first five minutes matter as much as the tests.**

Current state: v0.7.0 released (2026-11-03). The loop,
client, registry, event log, manifest, guardrails, retriever, Laya decision model,
MCP tools, OTel/Langfuse + MLflow exporters, eval CLI, web view with bearer auth
(read-only, or chat with in-page approvals when `--manifest` is passed), the
opt-in OpenAI-compatible `/v1` surface for LibreChat (`--openai-api`, ADR-0028),
subagents and opt-in parallel tool execution are implemented and tested (374
offline tests plus two browser tests; ruff + `mypy --strict` clean). All four
exit-criteria demos verified live.

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
10. **Non-goals stay non-goals** (ADR-0017). Agent memory and MCP serving are
    still out. Subagents (v0.6, ADR-0019), the read-only web view (v0.5,
    ADR-0018) and the opt-in chat write path (v0.7, ADR-0023) are in: extend
    those, do not reinvent them. The chat path is the only place the service
    writes, and it does so by calling `run_agent`, never by reimplementing it.

## Layout

```
rigging/
  types.py          canonical Message/ToolCall/Usage/ModelResponse (+ reasoning),
                    RunContext for loop-supplied tool scope (ADR-0024)
  protocols.py      every swappable interface; imports only types
  registry.py       decorators + entry-point discovery; resolve_all fails fast
  loop.py           the agent loop; invariants 2-4 live here
  events.py         SQLite append-only log, projections, JSONL export
  manifest.py       YAML load, JSON Schema validation, single-level defaults
  redact.py         the security boundary
  crypto.py         AES-GCM sealing for PII that has to be stored (ADR-0027)
  vault.py          encrypted per-session stores: sentinel maps (ADR-0027)
  pii_store.py      where a stored contact lives, and the AAD that binds it
  client/           OpenAI-compatible httpx client + hand-written SSE parser
  guardrails/       pii (redact + pseudonymise), injection, consequential_action, commitment
  retriever/        fts5 (works), qdrant (v0.3 stub)
  decide/           laya (v0.3 stub, advisory only)
  tools/            function -> Tool adapter, JSON Schema from annotations
  service/          FastAPI + SSE view of the event log (ADR-0018); chat.py
                    adds the opt-in write path and WebApproval (ADR-0023);
                    openai_api.py serves /v1 for a chat UI, no approvals (ADR-0028)
  subagents.py      spawn_<name> tools that run a child agent (ADR-0019)
  init_agent.py     `harness init`: the manifest interview, validated before writing
  cli.py            harness run|init|schema|tools|runs|escalations|chat|index|eval|calibrate|serve|pii
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
docs/red-team.md                   promptfoo scans: the target, the controls, the findings
redteam/                           promptfoo provider, configs and generated probes (v0.8)
scripts/seal_pii.py                one-off: seal plaintext contacts in a demo DB (ADR-0027)
scripts/bench.py                   models x repeats driver -> results/<run>/ (one
                                   directory per invocation, never overwritten)
scripts/langfuse_to_atif.py        Langfuse session -> ATIF v1.7 trajectory file
results/                           benchmark evidence, gitignored: one directory per
                                   bench.py invocation (`make bench` regenerates it)
tests/                             unit, SSE wire fixtures, loop invariants,
                                   and the opt-in browser tests (read + chat)
```

## Workflow

```bash
uv sync --extra dev          # env
make help                    # every target, every override, gateway choices
make check                   # ruff + mypy --strict + pytest (no network)
make test-browser            # opt-in: render the web view and run one turn in Chromium
redteam/run.sh controls      # opt-in, live: promptfoo known-good cases before a scan
redteam/run.sh scan          # ...and the unattended red team (needs npx + .env)
make bench                   # models x repeats -> results/<utc>/ (Langfuse traces by default)
uv run harness init          # interview -> agents/<name>/agent.yaml (validated at load)
uv run harness tools         # registry sanity
uv run harness run examples/hello/agent.yaml "hello"   # needs a gateway (see below)
```

- Work in the Unix environment (WSL/Linux). **Line endings must be LF** — a CRLF
  file breaks `make` and shell scripts.
- Do not touch `.venv/`, `*.db`, `runs/`, `results/`. They are gitignored; never
  commit them.
- Tests must not touch the network. Real-model runs are marked `-m live` and are
  opt-in. New behaviour needs a test in the same commit — a guardrail without a
  test asserting its verdict is not done.
- Commit messages: `area: imperative summary` (e.g. `loop: ask for a final answer
  at budget exhaustion`). One logical change per commit; the history is part of
  the portfolio.

## Gateway / models

The harness is gateway-agnostic; `RIGGING_BASE_URL` decides. Defaults assume an
OpenAI-compatible endpoint on `:20128` (OmniRoute, `make gateway`) or Ollama's own
`/v1` on `:11434`. The canonical demo's model is `deepseek/deepseek-v4.1-flash`
through the `openrouter` profile (needs `RIGGING_PROFILE_OPENROUTER_API_KEY`);
`qwen3:8b` with `PROFILE=ollama` is the fully local fallback. `beam:cloud` is no
longer the default: it returns empty completions often enough that the loop has
to fail the run (ADR-0016 amendment).

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
make trace MANIFEST=examples/b2b-support/agent.yaml PROMPT="I need switches"
make trace MODEL=qwen3:8b PROFILE=ollama   # ...against host Ollama; no gateway container needed
make demo EXPORT=langfuse PROMPT="What is the refund policy for ORD-1002?"
redteam/run.sh scan         # one trace per probe: named by plugin, tagged by posture
make web EXPORT=langfuse        # one trace per browser turn
make eval EXPORT=langfuse       # the b2b eval suite, one trace per case
make eval MODEL=qwen3:8b PROFILE=ollama    # the fully local path
```

`EXPORT=` (or `harness run|chat|serve --export langfuse`) overlays the manifest's
`observability.exporters` at assembly time; the shared demo manifest stays
key-free, so `make check`, replay and the browser tests need no credentials.
`PROFILE=` (or `harness eval --profile …`) does the same for the
`credential_profile`: the demo pins `openrouter`, so `PROFILE=ollama` is how a
local model id reaches `localhost:11434`.

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
parallel tool execution. v0.7 (2026-11-03) made the web view drivable — chat and
in-page approvals behind `--manifest` and a token (ADR-0023). Start from the
first unticked box above that.

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
