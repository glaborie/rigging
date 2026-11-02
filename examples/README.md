# Examples

## `b2b-support/` — the canonical demo agent

Target state: v0.3. The manifest, prompt and both skills are final; the tools and
the fixture database land with the v0.3 milestone (`seed.py` documents the shape).

It exercises every layer of the harness at once:

| Layer | How this example exercises it |
|---|---|
| tools | 5 tools, one of them `side_effecting` (`refund_order`) |
| registry | all capabilities resolved by name from the manifest |
| skills | two skills, index-only in the prompt, body fetched by `load_skill` |
| retrieval | policies via Qdrant (`search_policies`) |
| guardrails | PII on input/output, injection on tool results, approval threshold, commitment check |
| approval | `refund_order` cannot run without an operator answer |
| escalation | `escalate_to_human` produces a structured handoff |
| observability | spans per step, exported to Langfuse |

## `hello/` — the v0.1 smoke test

No tools, no retrieval, one prompt. Use this to verify a gateway is wired
correctly before blaming the demo agent.

## `subagents/` — delegation as a child run (v0.6)

`agent.yaml` declares one subagent (`researcher.yaml`) and gets a
`spawn_researcher(task)` tool. The child is a **run**, not a subprocess: same
event log, same session, `runs.parent_run_id` set, its own model, prompt and
budget (ADR-0019).

```bash
uv run harness run examples/subagents/agent.yaml \
  "Use the researcher to explain what a vector database is, then summarise it in one sentence." --yes
uv run harness runs          # the child is indented under the parent
make serve                   # both runs, the child labelled "child of …"
```

`--yes` is needed because `spawn_*` tools are side-effecting: delegation spends
tokens, so it goes through the same approval gate as a refund. The child never
sees the parent's conversation — only the task string.
