# AGENTS.md

Operating instructions for any coding agent working in this repository. Read this
file, then `docs/architecture.md`, then the relevant ADR **before** writing code.

## What this is

`rigging` is a modular agent harness. A YAML manifest names capabilities; a
registry resolves them; every layer (gateway client, tools, guardrails, retriever,
decision model, exporters) is a small protocol with swappable implementations.
The project is a portfolio piece: **the code, the ADRs and the demo are the
deliverable, and a reviewer's first five minutes matter as much as the tests.**

Current state: v0.1 in progress. The loop, client, registry, event log, manifest
and guardrail set are implemented and tested (27 tests, ruff + `mypy --strict`
clean). Not yet run against a live gateway. Nothing past v0.1 exists beyond
interfaces. The authoritative task list is `docs/milestones.md` — work from it,
do not invent scope.

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
   extra (`[qdrant]`, `[laya]`, `[langfuse]`, `[mcp]`, `[service]`).
8. **No LLM-as-judge** in guardrails or evals (ADR-0009, ADR-0017).
9. **No silent feature: the CLI says what is not built.** Unimplemented commands
   print the milestone and exit 2.
10. **Non-goals stay non-goals** (ADR-0017). If a task seems to need agent
    memory, subagents, a web UI, parallel tool execution or MCP serving, stop and
    ask — the answer is probably "later milestone".

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
  cli.py            harness run|schema|tools|chat|index|eval
rigging/schema/agent.schema.json   the manifest contract (authoritative)
docs/architecture.md               layers, loop sequence, event schema
docs/adr/                          one page per decision, numbered
docs/milestones.md                 the task list; definitions of done
examples/b2b-support/              the canonical demo (manifest + skills final)
tests/                             unit, SSE wire fixtures, loop invariants
```

## Workflow

```bash
uv sync --extra dev          # env
make check                   # ruff + mypy --strict + pytest (no network)
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
`ollama signin`); `qwen3:8b` is the fully local fallback and **the demo must pass
its eval suite on the weakest documented model**.

## Definition of done

A task is done when: the behaviour exists behind the documented protocol; there is
a test that fails without it (and an event type if it changes run state); `make
check` is green; the affected doc or ADR says what changed; and no user-facing
text claims something the code does not do.

## Where to start

`docs/milestones.md` → the first unticked box. Right now that is the v0.1 exit
criterion: run `examples/hello/agent.yaml` against a live gateway and fix whatever
the wire format disagrees about. Record the transcript under `examples/` when it
works.

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
