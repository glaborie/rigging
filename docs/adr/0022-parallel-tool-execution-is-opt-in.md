# ADR-0022: Parallel tool execution is opt-in, approval stays serial

**Status:** Accepted (supersedes the parallel-tool clause of ADR-0017)

## Context

A model can emit several tool calls in one response. The loop runs them one at a
time: correct, and slow when the tools are independent network calls (a retriever
plus two lookups). v0.6 asks for parallelism behind a flag. Four invariants are
at risk:

1. **Approval.** A side-effecting tool must be approved *before* it runs. A
   terminal prompt cannot be shown from four threads at once.
2. **The event log.** `EventLog` holds one SQLite connection, not shared across
   threads. Concurrent appends would corrupt `seq`.
3. **Message order.** The model expects one `tool` message per `tool_call_id`.
4. **Tool safety.** Tools are user code: a retriever or SDK client may not be
   thread-safe.

## Decision

`parallel_tools: true` in the manifest (default `false`) changes one thing: the
`tool.run` calls of a **single model response** run in a bounded
`ThreadPoolExecutor`. Everything else stays on the main thread and in declared
order.

The step becomes three phases:

1. **Prepare (serial, declared order).** Append `tool_call`, start the span, run
   `on_tool_call` guardrails, and ask for approval for every side-effecting call.
   A denial is recorded immediately; the rest still proceed.
2. **Execute (concurrent).** Only approved calls, only `tool.run`, capped at 8
   workers. Exceptions become `is_error` results (hard rule 3, now also for
   third-party `Tool` implementations that do not catch their own).
3. **Finish (serial, declared order).** End spans, apply `on_tool_result`, append
   `tool_result` events, build messages. The log and the message list therefore
   keep `seq` and call order deterministic regardless of completion order.

A `tool_batch` event records the mode and the call names, so an eval can tell a
parallel step from a sequential one without inferring it from timestamps.

## Consequences

Independent I/O overlaps, so a slow tool no longer delays a fast one's
execution. Events are written after the batch, so per-tool completion appears in
`duration_ms` rather than in event timestamps. Cost: the flag is a promise the
operator makes about their tools' thread safety, tools that share state (a
retriever connection, a rate limiter) can misbehave when parallel, and a batch
that takes longer than the slowest call still blocks the step — parallelism here
is not streaming.

## Alternatives rejected

- **Parallel by default**: silently changes the thread-safety contract for every
  existing tool.
- **Concurrent approvals**: unanswerable in a terminal, and it would let two
  consequential calls race for one decision.
- **Writing events from worker threads**: needs a second connection or a lock per
  append; the log is the security boundary and stays single-writer.
- **`asyncio`**: the loop, client and tools are synchronous by design (ADR-0001);
  a thread pool is the smallest change that keeps that contract.
