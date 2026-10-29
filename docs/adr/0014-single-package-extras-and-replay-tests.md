# ADR-0014: One package with extras; replay-based integration tests

**Status:** Accepted

## Context

A portfolio project's first five minutes decide whether anyone reads the code.
A monorepo of `core`/`providers`/`tools` is conceptually cleaner and costs an
afternoon of packaging friction per extra. Testing an agent against live models is
slow, flaky and non-deterministic.

## Decision

Single installable package `rigging` with extras (`[qdrant]`, `[langfuse]`,
`[laya]`, `[mcp]`, `[service]`). `uv` for env and lock, `ruff` + `mypy --strict`
on public modules, `pytest` with three tiers: unit, wire-level SSE contract tests
over recorded fixtures (`pytest-httpx`), and integration tests that replay a
recorded transcript end to end with no network. `--replay` runs an agent against a
saved transcript; `-m live` is the opt-in real-model marker.

## Consequences

CI is deterministic and free; the harness is debuggable offline; and replay is
the stepping stone to the eval layer. Cost: fixtures must be re-recorded when the
wire format changes — which is exactly when you want to look at it.

## Alternatives rejected

Monorepo: rejected for friction. Unit-only tests: rejected — the SSE parser is
where bugs live. Live-model CI: rejected for cost and flakiness.
