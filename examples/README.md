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
