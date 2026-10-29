# Milestones

Each milestone is shippable on its own. The go-public gate is deliberately early:
the first point at which a stranger can run it and understand it.

**Scaffold state (v0.0):** the ticked v0.1 items below are implemented and covered
by tests (27 passing, ruff + mypy --strict clean). The v0.1 exit criteria — a run
against a live gateway — is **not** met yet, and nothing past v0.1 exists beyond
interfaces and docs.

## v0.1 — the loop is real

- [x] `types.py`: Message, ToolCall, ToolResult, Usage, ModelResponse (with `reasoning`)
- [x] `protocols.py`: LLMClient, Tool, Guardrail, ApprovalPolicy, Retriever, DecisionModel, Exporter, SubagentRunner
- [x] `registry.py`: decorators + `discover_entry_points()` (opt-in)
- [x] `client/openai_compat.py`: httpx, SSE parser, retries, `usage` mapping, reasoning passthrough
- [x] `loop.py`: the sequence in `docs/architecture.md` §3, tool errors as messages
- [x] `events.py`: SQLite schema, append-only writer, redactor boundary
- [x] `manifest.py`: load + JSON Schema validation + defaults merge
- [x] `cli.py`: `harness run`, `harness schema`; streaming to terminal
- [x] tests: SSE wire fixtures (chunk reassembly, tool-call deltas, malformed, 429), loop unit tests with a fake client

**Exit criteria — v0.1 DONE (2026-10-29):** live runs through the full stack
(harness loop -> OmniRoute -> freellmapi -> codestral, and `beam:cloud` via ollama
cloud) completed end-to-end with the full event trail in SQLite; the **two-tool
task** (`examples/two-tools/`, `current_time` + `sha256_hex`) completed in 4 steps
with both tools called and a verifiable digest. Transcript:
`examples/hello/transcript-codestral.jsonl`; malformed-SSE covered by
`tests/test_client.py`; core tools covered by `tests/test_core_tools.py`.
Remaining nice-to-have, not gating: a run against a pulled `qwen3:8b`
(`ollama pull qwen3:8b`).

## v0.2 — knowledge and memory  ⇒ GO PUBLIC HERE  ← NEXT

- [ ] `skills.py`: filesystem discovery, frontmatter parsing, index-only system prompt
- [ ] `tools/builtin.py`: `load_skill`, `escalate_to_human`
- [ ] sessions table, `harness chat` REPL (streaming, `/transcript`, `/escalate`)
- [ ] JSONL export + `harness run --replay`
- [ ] README quickstart verified from a clean clone on Linux

**Publish gate:** 5-minute zero-API-key quickstart · green CI on `main` · committed
demo transcript under `examples/` · architecture diagram · no TODOs in user docs ·
repo flipped public.

## v0.3 — safety and the demo

- [ ] four guardrails: PII redaction, prompt-injection heuristic, consequential-action
      gate, out-of-policy-commitment check
- [ ] `ApprovalPolicy`: CLI prompt, scripted, auto-deny
- [ ] `retriever/qdrant.py` + `retriever/fts5.py`, `harness index`
- [ ] `decide/laya.py` behind `[laya]`, advisory only
- [ ] `examples/b2b-support/`: seeded SQLite fixture DB, 2 skills, 5 tools, agent.yaml
- [ ] `make run MANIFEST=examples/b2b-support/agent.yaml ...` (compose profile `default`:
      omniroute; zero credentials); `make help` documents every target and override

**Exit criteria:** a refund request is blocked pending approval, an out-of-policy
commitment is caught, a vague request escalates with a structured handoff.

## v0.4 — observability and evaluation

- [ ] `Exporter` + OTel `gen_ai.*` spans, OTLP → Langfuse behind `[langfuse]`
- [ ] `harness eval <suite.yaml>` over event-log predicates; cost/latency/pass table
- [ ] `tests/data/decisions.jsonl` (~40 labelled cases) scoring Laya's calibration
- [ ] MCP tool source behind `[mcp]`, namespaced tools
- [ ] 6–8 golden eval cases for the demo agent

## v0.5 — the professional finish

- [ ] manifest JSON Schema published + `harness schema` documented
- [ ] entry-points discovery on by default, with a documented third-party example
- [ ] `docs/architecture.md` final, ADRs complete, writeup published
- [ ] 60-second terminal recording in the README
- [ ] stretch: FastAPI + SSE web widget reading the same SQLite sessions

## v0.6 — scale-out (optional)

- [ ] `SubagentRunner`: child runs with own manifest/budget, `parent_run_id` wired
- [ ] FastAPI service + auth (bearer keys or OIDC) designed as its own ADR
- [ ] MLflow exporter as the second `Exporter`
- [ ] parallel tool execution behind an opt-in flag

## Standing rules

1. The demo must pass its eval suite on the **weakest documented model** (`qwen3:8b`).
2. No feature ships without an event type and a test that asserts on it.
3. Every new abstraction needs an ADR or it is not merged.
