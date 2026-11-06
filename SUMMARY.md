# rigging — codebase summary

## What `rigging` is

A **modular agent harness** — a Python library plus CLI (`harness`) and an optional FastAPI web view — built around one claim: *every layer of an agent harness should be a swappable protocol, resolved by name from a YAML manifest.*

It deliberately does **not** own a layer (unlike LangGraph, CrewAI, etc.). It supplies the protocols, a registry, a replayable event log, approval gates, guardrails, retrieval, tracing and an eval harness, and lets you assemble an agent out of them. It is explicitly a portfolio piece (v0.8.0, 2026-11-05): ~8,800 lines of Python, 28 ADRs, 379 tests collected (374 offline), ruff + `mypy --strict` clean.

## Architecture

```
agent.yaml (JSON-Schema validated)
     │
     ▼
Registry (decorators + importlib entry-points)  → tools, skills, guardrails,
     │                                            retrievers, decision models
     ▼
Agent loop  ──► LLMClient (OpenAI-compatible HTTP, hand-written SSE)
     │           Guardrail chain: allow | deny | transform
     │           ApprovalPolicy (auto_deny by default, for side_effecting tools)
     │           Tools (JSON-Schema from type hints)
     │           Retriever (FTS5/BM25 or Qdrant)
     └────────► SQLite append-only event log (the source of truth)
                Exporter protocol (OTel→Langfuse, MLflow, console)
```

**Five hard invariants** are the actual design: the core never imports a vendor (only `httpx` talking OpenAI-compatible HTTP); every step is an event; a tool can never crash a run (all failures become `is_error` tool messages); a side-effecting tool can never run unapproved; and every payload passes `redact_event()` before insert — the event log is treated as a security boundary.

## What it implements

| Area | Reality in the code |
|---|---|
| Loop | Streaming, reasoning capture, budget limits (steps/tokens/USD/wall-clock), final-answer call at exhaustion, subagents as child runs, opt-in parallel tool calls |
| Tools | Decorator-based `Tool` adapter, JSON Schema from annotations, `RunContext` (session/run ids) injected into tool signatures |
| Guardrails | 6 built-ins: PII redact, PII pseudonymise (per-run sentinel maps), prompt injection, consequential action, commitment, refund-window (domain demo) |
| Approval | CLI `y/N`, scripted, and in-page web approval; `auto_deny` default |
| Retrieval | FTS5/BM25 (the credential-free default) and a working Qdrant retriever using Ollama `nomic-embed-text` |
| Events | SQLite append-only log, projections, JSONL export, Langfuse session mapping |
| Observability | OTel `gen_ai.*` spans → Langfuse v4 (observations-first, OTLP/HTTP), MLflow via plain REST (no SDK), console |
| PII at rest | AES-GCM sealing (`crypto.py`, `vault.py`, `pii_store.py`), `harness pii open` to unseal |
| Service | FastAPI + SSE read-only event-log view; opt-in chat with in-page approvals behind `--manifest` + bearer token; opt-in OpenAI-compatible `/v1` for LibreChat (no approvals there) |
| MCP | Consumes MCP servers as a tool source (ADR-0013: consumed, not served) — a stated non-goal is agent memory and MCP serving |
| Decision model | `DecisionModel` protocol with OpenRouter / Ollama / FreeLLMAPI adapters; **the Laya adapter is a stub that raises** (`RiggingError`) |
| Eval / bench | `harness eval` over event-log predicates (no LLM-as-judge, per ADR-0009/0017), `scripts/bench.py` for model×repeat benchmarks, ATIF trajectory export |
| Red team | `redteam/` — promptfoo configs, a custom provider, controls run + unattended scan |

CLI: `run`, `chat`, `init` (interactive manifest interview), `schema`, `tools`, `runs`, `escalations`, `index`, `eval`, `calibrate`, `serve`, `pii`.

## Stack and dependencies

**Core (deliberately tiny):** `httpx` (with a hand-written SSE parser, on purpose), `pydantic` v2, `pydantic-settings`, `pyyaml`. Python ≥3.11, `hatchling` build, `uv` for env, stdlib `sqlite3` for the event log.

**Optional extras** — everything heavier is opt-in: `[qdrant]`, `[langfuse]` (OTel SDK + OTLP HTTP exporter), `[mcp]`, `[laya]` (torch/transformers), `[service]` (FastAPI + uvicorn), `[browser]` (Playwright), `[vault]` (`cryptography`).

**Tooling:** pytest (+httpx, asyncio), ruff, mypy strict, jsonschema; promptfoo/Node for red teaming. No `openai` SDK, no LangChain, no `tiktoken` — those are explicitly rejected in ADRs (gateway `usage` is authoritative for cost).

## Demos

`examples/b2b-support` is the canonical one: a B2B procurement/refunds agent with 12 tools, 2 markdown skills, FTS5 retrieval, 5 guardrail hooks, real approvals, a seeded SQLite `demo.db`, and sealed PII. Other examples: `hello` (replay transcript), `subagents`, `parallel`, `mlflow`, `observability`, `third-party-tools`, `librechat`.

## Honest caveats

- Laya is declared but unimplemented — advisory by design until calibration data justifies it (ADR-0009).
- The `/v1` surface for chat UIs has **no** approval path, so it is not for side-effecting agents.
- `make check` is offline by rule: observability is only proven by `make trace`, and browser behaviour only by `make test-browser`.
