# ADR-0015: The manifest is flat, validated by JSON Schema, with no inheritance

**Status:** Accepted

## Context

Config systems grow `extends:` and templating because someone had three agents
sharing a model line. Then the effective config of any agent requires reading four
files, and the schema stops being checkable.

## Decision

One YAML file per agent, flat and fully explicit, validated against
`rigging/schema/agent.schema.json`. Sharing happens through a `harness.yaml` defaults file
merged at load (documented, single-level, no chains). No `extends`, no `mixins`, no
Jinja. Env interpolation is limited to secret references (`${OPENAI_API_KEY}`),
never to config structure. `harness schema` prints the schema; `harness run`
validates before assembling.

## Consequences

Any agent's full behaviour is one file. Editors and reviewers get real completion
and real errors. Cost: repetition across agents — accepted until three agents
genuinely share structure, at which point defaults (already there) usually
suffice.

## Alternatives rejected

`extends`/mixins: deferred, not rejected forever. Jinja templating: rejected —
unreadable stack traces for a config file. TOML/JSON instead of YAML: YAML chosen
for comments in multi-line prompts and for familiarity.
