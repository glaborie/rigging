# ADR-0003: Capabilities register via decorators, discovered via entry points

**Status:** Accepted

## Context

Tools, guardrails, retrievers and decision models must be addressable by name
from a manifest without the loop importing them. Two audiences want different
things: the author writing an agent (wants `@tool` in one file) and the consumer
of a third-party package (wants `pip install` to add capabilities with no code
change).

## Decision

`Registry` holds name → implementation, populated by decorators at import time.
Entry-points discovery is triggered automatically during `assemble()` and
disovered capabilities are logged to stderr. To suppress discovery, pass an
explicit `Registry()` instance to `assemble()` or `run_agent()`. Assembly
resolves every name in the manifest and fails fast with the full list of unknown
names — no silent skipping.

## Consequences

`print(registry)` is a complete debugging story in v0.1, and the same mechanism
scales to third-party packages in v0.5. Cost: entry-point loading is implicit
magic, logged to stderr during assembly for visibility, and name collisions
raise instead of last-wins.

## Alternatives rejected

Central `if/elif` dispatch: unscalable. Decorator-only: no distribution story.
Entry-points-only: every test needs an installed package, which makes the core
untestable.
