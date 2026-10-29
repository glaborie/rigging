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

- [x] `skills.py`: filesystem discovery, frontmatter parsing, index-only system prompt
- [x] `tools/builtin.py`: `load_skill`, `escalate_to_human`
- [x] sessions table, `harness chat` REPL (`/transcript`, `/escalate`; per-turn runs share one session). Verified live: two-turn run through the gateway recalled a codeword given in the first turn
- [x] JSONL export + `harness run --replay`. Verified live: a recorded two-tool run replayed byte-identical with the API base at a dead port (tests: `tests/test_replay.py`)
- [x] README quickstart verified from a clean clone on Linux (2026-10-29: fresh clone, install 1 s, 34 tests green, live run in 3 s)

**Publish gate — met except the flip (2026-10-29):** 5-minute zero-API-key quickstart ✓
(verified from a clean clone) · green CI on `main` ✓ · committed demo transcript ✓ ·
architecture diagram ✓ · no TODOs in user docs ✓ · tagged `v0.2.0` with release notes ✓.
The repo stays **private** for now by decision; flipping it public is the remaining step
(then add branch protection, which needs a public repo or Pro).

## v0.3 — safety and the demo

- [x] four guardrails: PII redaction, prompt-injection heuristic, consequential-action
      gate, out-of-policy-commitment check (all wired into the demo manifest)
- [x] `ApprovalPolicy`: CLI prompt, scripted, auto-deny
- [x] `retriever/qdrant.py` (ollama `nomic-embed-text`, behind `[qdrant]`) + `retriever/fts5.py`
      (reference, zero-credential); `harness index` (no-op for fts5, embeds+upserts for qdrant)
- [x] `decide/laya.py` behind `[laya]`, advisory only
- [x] `examples/b2b-support/`: seeded SQLite fixture DB (40 products, 12 orders, 19 policy
      chunks — deterministic seed=7), 2 skills, 5 tools, agent.yaml
- [x] `make run MANIFEST=examples/b2b-support/agent.yaml ...` — zero credentials: fts5
      retriever over the seeded demo.db, model via the local OmniRoute stack

**Exit criteria — MET, verified live 2026-10-29 (beam:cloud via OmniRoute):**
1. *Refund blocked pending approval:* refund_order(112.50, ORD-1002) →
   `approval_requested` → operator answered "n" → `approval_verdict: approved=false`
   → tool denied → model loaded the `escalation-summary` skill → structured handoff.
2. *Over-authority caught:* a $1,350 refund attempt was denied by the
   consequential-action guardrail ($500 limit) before execution → escalated with reason
   `needs_approval`. (Bonus honesty: a deliberately wrong amount, $186.40, was caught
   by the model reconciling against lookup_order and escalated instead of processed.)
3. *Out-of-policy commitment:* permanent-discount demand refused with the CMP-001
   passage cited, then escalated.
4. *Human demanded:* explicit "get me a human manager" → structured handoff with
   verified order facts (reason=customer_requested).

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
