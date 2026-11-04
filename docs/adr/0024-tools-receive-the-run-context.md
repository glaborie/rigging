# ADR-0024: Tools receive the run context, not a conversation id from the model

**Status:** Accepted (v0.8)

## Context

The b2b demo's basket was built with `add_to_basket(..., basket_id="")`: the
first call minted an id and the model had to repeat it in every later call. That
works in a single-threaded exchange and breaks as soon as the model emits one
batch of calls: with parallel tool execution (ADR-0022), two `add_to_basket`
calls in the same response each saw an empty `basket_id` and started **their
own** basket. The model had to notice, move a line and clear the spare — extra
steps, extra tokens, and a transcript that looks like a bug. The same shape will
appear for any conversation-scoped state (a draft ticket, a running total).

The missing input is identity: the tool needs to know which session it is
serving. Three ways to give it one:

- an ambient global or `contextvars.ContextVar` set by the loop;
- put the session id in the prompt and have the model pass it;
- a context object the loop passes to the tool.

Ambient state is invisible at the call site and does not follow a job into a
`ThreadPoolExecutor` without copying the context by hand. Letting the model
carry the id makes the model the authority on which session it is in — the same
class of mistake as trusting a model with an approval decision.

## Decision

`Tool.run(call, ctx)` takes a `RunContext` (a frozen dataclass in
`rigging/types.py`) holding `session_id` and `run_id`. The loop creates it right
after `start_run` and passes it down; the parallel executor passes the same
object as an argument, so threads need no thread-local magic.

`FunctionTool` injects it into a function parameter annotated `RunContext` and
**excludes that parameter from the generated JSON schema**. The model never sees
the parameter, never sends it, and cannot choose its session. The demo's basket
tools therefore take no id at all: `add_to_basket(sku, qty)`,
`view_basket()`, `update_basket_item(sku, qty)`, `clear_basket()`,
`submit_basket()` — one basket per session, stored in SQLite beside the orders.

A tool that does not need conversation state ignores `ctx` (`McpTool`, the
`spawn_*` tools, which already capture the session when they are built). A tool
called directly without a context (`FunctionTool.run(call)`) that does need one
returns a structured tool error, never an exception into the loop.

The context is **not** an authorization mechanism. It names where a call
happens; approval and guardrails still decide whether it may.

## Consequences

- Third-party `Tool` implementations must accept the second argument. This is a
  breaking protocol change; the repo has three implementations and all of them
  are updated, but entry-point tools written against v0.7 will not type-check
  (and will fail at call time) until they do.
- Session state outlives a turn but not the database: a basket survives a
  restart of the process and a server restart's session id, because `harness
  serve` rebuilds history from the log and reuses the session.
- The demo loses `basket_id` from its API. The pre-ADR-0024 tables are renamed to
  `baskets_legacy` / `basket_items_legacy` on first use rather than dropped.
- A test that exercises a basket tool must pass a `RunContext`; that is the
  point — the dependency is visible.

## Alternatives rejected

**`contextvars` set by the loop.** No protocol change, but invisible at the call
site, and `ThreadPoolExecutor` does not propagate context without
`copy_context().run` per job — an easy thing to get subtly wrong in exactly the
parallel path this ADR exists to fix.

**Session id as a tool argument.** The model would be the source of identity and
could pass another session's id; the schema would advertise an internal id as
model input.

**One "current basket" row.** Works for a single user and silently mixes two
concurrent sessions — the failure is a wrong order, which is worse than an
extra step.
