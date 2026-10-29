# rigging

> Capabilities are declared in a manifest and discovered through a registry.
> The loop, the gateway, the guardrails, the observability backend and the
> retriever are all swappable implementations of small protocols.

**Status: v0.1 in progress.** The loop, the OpenAI-compatible client (streaming,
tool-call deltas, retries, reasoning passthrough), the registry, the SQLite event
log and manifest validation are implemented and tested — 27 tests, ruff and
`mypy --strict` clean, SSE covered by recorded wire fixtures. It has **not** been
run against a live gateway yet, and everything past v0.1 is interfaces and
decisions only. See [`docs/milestones.md`](docs/milestones.md).

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
| Registry + entry-point discovery | core | ● | | | | | |
| Skills (progressive disclosure) | filesystem | | ● | | | | |
| SQLite event log + sessions | core | | ● | | | | |
| `harness chat` REPL | core | | ● | | | | |
| Guardrails (4 built-ins) | decorator | | | ● | | | |
| Approval policy (side-effecting tools) | decorator | | | ● | | | |
| Escalation to human | tool | | | ● | | | |
| Retrieval (Qdrant default, FTS5 reference) | decorator | | | ● | | | |
| Laya decision model (advisory) | decorator | | | ● | | | |
| OTel → Langfuse exporters | core | | | | ● | | |
| `harness eval` | core | | | | ● | | |
| MCP servers as a tool source | entry point | | | | ● | | |
| Manifest JSON Schema + docs | -- | | | | | ● | |
| Web widget (stretch) | extra | | | | | ◐ | |
| Subagents (`SubagentRunner`) | protocol slot | | | | | | ● |
| FastAPI service + auth | extra | | | | | | ● |

`●` shipped · `◐` stretch goal

## Quickstart

```bash
# 1. Model access — pick one
ollama signin                              # beam:cloud, free at time of writing
# or the fully local path:
ollama pull qwen3:8b

# 2. Install
uv sync

# 3. Run an agent (v0.1: the no-tools smoke agent)
uv run harness run examples/hello/agent.yaml "Say hello in one sentence." --model freellm/codestral

# The b2b-support agent (5 tools, skills, retrieval, guardrails) lands in v0.3 —
# its manifest is final but its tools are not implemented yet, so assembly
# correctly fails fast. See docs/milestones.md.
```

No API key is required for either path. The gateway is OpenAI-compatible HTTP,
so pointing `RIGGING_BASE_URL` at OmniRoute, vLLM, LM Studio or Ollama's own
`/v1` is all it takes to switch providers.

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
docs/
  architecture.md   how the layers fit and why
  milestones.md     v0.1 → v0.6 with the go-public gate
  adr/              one page per decision, numbered
rigging/schema/
  agent.schema.json machine-checkable manifest contract
examples/b2b-support/   the canonical demo agent
tests/            unit + SSE wire fixtures + replay integration
```

## Documentation

- [`AGENTS.md`](AGENTS.md) — operating instructions for a coding agent working in this repo

- [`docs/architecture.md`](docs/architecture.md) — layers, loop sequence, event schema
- [`docs/adr/`](docs/adr/) — 17 decisions with the alternatives that were rejected
- [`docs/milestones.md`](docs/milestones.md) — what ships when, and the publish gate

## Non-goals

Deliberately not built (see [ADR-0017](docs/adr/0017-deliberate-non-goals.md)):
agent long-term memory, multi-agent orchestration in v1, serving MCP, LLM-as-judge
guardrails or evals, user auth before the service milestone, a web UI before v0.5,
model fine-tuning, parallel tool execution, Windows-native support, a plugin
marketplace.

## License

MIT. See [`LICENSE`](LICENSE).
