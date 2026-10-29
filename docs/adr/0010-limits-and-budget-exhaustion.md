# ADR-0010: Limits are enforced in-loop; exhaustion asks for a final answer

**Status:** Accepted

## Context

Agents loop. Without limits a bad prompt becomes an unbounded bill; with hard
raises a limit breach becomes a lost conversation.

## Decision

`limits: {max_steps: 20, max_tokens, max_cost_usd, max_wall_clock_s}` checked
before each model call. On exhaustion the loop disables tools and asks the model
for a final answer; if it still will not conclude, the run ends with a structured
`BudgetExhausted` carrying the partial transcript. Retries only for transport
errors (429/5xx/timeout), max 3, capped exponential backoff with jitter, each
retry recorded as an event. `CancelledError` propagates and the partial transcript
is already durable.

## Consequences

Every run terminates with a usable answer or a structured, inspectable failure.
Cost: `max_tokens` is necessarily post-hoc (it is enforced from cumulative gateway
`usage`), so a single very long generation can overshoot by one call —
documented, and the wall-clock limit covers the hang case.

## Alternatives rejected

Hard raise: rejected, loses the conversation. Advisory-only limits: rejected, an
unbounded agent. Local tokenizer pre-estimation (`tiktoken`): rejected — wrong for
non-OpenAI models, and provider `usage` is authoritative.
