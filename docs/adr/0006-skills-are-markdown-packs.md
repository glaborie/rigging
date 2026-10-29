# ADR-0006: Skills are markdown packs loaded by progressive disclosure

**Status:** Accepted

## Context

Tools and skills must earn separate existence. A tool is a function call. A skill
is procedural knowledge — how to judge a posting, how to format an escalation. If
every skill were inlined in the system prompt, a ten-skill agent would spend
30k tokens before the user said anything.

## Decision

A skill is a directory with `SKILL.md` (YAML frontmatter: `name`, `description`)
plus optional `references/` and `scripts/`. At assembly, only `name` + `description`
enter the system prompt as an index. A built-in `load_skill(name)` tool returns the
full body on demand. Resolution: filesystem directory first, installed packages
second.

## Consequences

Context cost scales with skill count as ~50 tokens each instead of ~3k. Cost: one
extra turn when a skill is needed, and the model can pick the wrong skill — which
is measurable in the eval suite (`skill_loaded` events vs expected).

## Alternatives rejected

Skills as code plugins: indistinguishable from tools, no knowledge capture.
Static template splicing: defeats the purpose. Vector-search skill routing: rejected
for a corpus of five skills.
