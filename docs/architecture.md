# Architecture

> Status: v0.0 scaffold. The interfaces below exist in code; implementations are
> stubs until their milestone.

## 1. The claim

Every layer of an agent harness is replaceable if you define it as a protocol and
resolve implementations by name. `rigging` is the smallest harness that takes
that claim seriously: the loop knows about protocols, never about vendors.

```
                    ┌──────────────────────────── agent.yaml ───────────────────────────┐
                    │ model · credential_profile · system_prompt · tools · skills       │
                    │ guardrails{on_input,on_tool_call,on_output} · limits · retriever  │
                    └───────────────────────────────┬──────────────────────────────────┘
                                                    │ validate (JSON Schema) + resolve names
                    ┌───────────────────────────────▼──────────────────────────────────┐
                    │                            Registry                              │
                    │  tools · skills · guardrails · retrievers · decision models      │
                    │  source 1: decorators in-process    source 2: entry points        │
                    └───────────────────────────────┬──────────────────────────────────┘
                                                    │
   ┌──────────────────────┐   ┌─────────────────────▼──────────────────────┐   ┌────────────────────┐
   │  LLMClient protocol  │◄──┤              Agent loop (v0.1)             ├──►│ Exporter protocol  │
   │  OpenAI-compatible   │   │  on_input → model ⇄ tool results → on_output│   │ OTel → Langfuse    │
   │  httpx, SSE          │   └───────┬───────────────────────┬────────────┘   └─────────┬──────────┘
   └──────────────────────┘           │                       │                          │
                              ┌───────▼────────┐     ┌────────▼─────────┐      ┌─────────▼─────────┐
                              │ Tool protocol  │     │ Guardrail chain  │      │  Event log        │
                              │ + side_effecting│    │ allow|deny|transform    │ SQLite, append-only│
                              └───────┬────────┘     └────────┬─────────┘      └───────────────────┘
                                      │                       │
                              ┌───────▼────────┐     ┌────────▼─────────┐
                              │ Retriever      │     │ ApprovalPolicy   │
                              │ Qdrant | FTS5  │     │ CLI y/N | script │
                              └────────────────┘     └──────────────────┘
```

## 2. Protocols

| Protocol | Shape | Implementations | Milestone |
|---|---|---|---|
| `LLMClient` | `stream(request) -> Iterator[ModelDelta]`, `complete(request) -> ModelResponse` | `OpenAICompatClient` (httpx) | v0.1 |
| `Tool` | `name`, `description`, `json_schema`, `side_effecting`, `run(args) -> ToolResult` | decorator-registered functions | v0.1 |
| `SkillResolver` | `discover() -> list[SkillMeta]`, `load(name) -> Skill` | filesystem + packages | v0.2 |
| `Guardrail` | `on_input`, `on_tool_call`, `on_tool_result`, `on_output` → `Allow | Deny | Transform` | 4 built-ins | v0.3 |
| `ApprovalPolicy` | `request(tool, args, context) -> Approve | Deny` | CLI prompt, scripted (tests) | v0.3 |
| `Retriever` | `search(query, k) -> list[Chunk]`, `requires_indexing` | Qdrant (default), FTS5 (reference) | v0.3 |
| `DecisionModel` | `decide(state, questions) -> typed answers + probabilities` | Laya (advisory) | v0.3 |
| `Exporter` | `span(name, attrs)`, `event(...)` with `gen_ai.*` attrs | OTel→Langfuse, MLflow | v0.4 / v0.6 |
| `SubagentRunner` | `spawn(spec, task) -> SubagentResult` | not implemented | v0.6 |

Every protocol is defined in `rigging/protocols.py`, one screen each, with no
imports from the rest of the package. That rule is what makes the swappability
real rather than aspirational: an implementation can be written and tested
without touching the loop.

## 3. The loop (v0.1 contract)

```
spec = load_manifest(path)            # JSON Schema validated
agent = assemble(spec, registry)      # names → implementations, fail fast on unknown
run  = eventlog.start_run(spec_hash)  # parent_run_id reserved for v0.6

messages = [system(spec.system_prompt + skill_index), user(prompt)]
for step in range(limits.max_steps):
    guard input   → on_input(chain)             may deny | transform
    check budgets → steps | tokens | cost | wall-clock   (pre-call)
    response = client.stream(request)           deltas → CLI, accumulate text+tool_calls+reasoning
    if no tool_calls: final answer → on_output(chain) → return
    for call in response.tool_calls:            sequential
        verdict = on_tool_call(chain, call)     may deny | transform args
        if call.side_effecting:
            approval = policy.request(call)     deny → tool message "denied by policy"
        result = tool.run(args)                 any exception → tool message, never a crash
        result = on_tool_result(chain, result)  untrusted content inspected here
        messages += tool message
# budget exhausted
final = client.complete(request, tools=[])      one last call, tools disabled
return FinalAnswer(final) or BudgetExhausted(partial=run)
```

Three invariants fall out of this and are enforced by tests:

1. **A tool can never crash the run.** Exceptions, invalid args, timeouts and
   denials all become structured `tool` messages the model can recover from.
2. **A side-effecting tool can never execute unapproved.** Approval is enforced
   by the loop, not by the tool's good behaviour.
3. **Every step is an event.** If it is not in `run_events`, it did not happen.

## 4. Event log

Append-only. The event log is the source of truth; every other view is a query.

```sql
CREATE TABLE run_events (
  run_id      TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  ts          REAL NOT NULL,
  type        TEXT NOT NULL,   -- run_started | input_received | guardrail_verdict
                               -- model_request | model_delta | reasoning_delta
                               -- model_response | tool_call | approval_requested
                               -- approval_verdict | tool_result | budget_check
                               -- escalation | output_verdict | run_finished | error
  payload     TEXT NOT NULL,   -- JSON, redacted before insert
  span_id     TEXT,
  parent_run_id TEXT,          -- reserved: v0.6 subagents
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE sessions (session_id TEXT PRIMARY KEY, created_at REAL, title TEXT, spec_hash TEXT);
CREATE TABLE runs     (run_id TEXT PRIMARY KEY, session_id TEXT, started_at REAL,
                       finished_at REAL, status TEXT, spec_hash TEXT, model TEXT, parent_run_id TEXT);
```

Derived views: `messages` (from `input_received`, `model_response`, `tool_result`),
`verdicts` (from `*_verdict`), `usage` (from `model_response.usage`), and the
replayable transcript exported to `runs/<id>.jsonl`.

**The log is a security boundary.** No payload reaches SQLite without passing
`redact_event()`; a test asserts that a secret-shaped string in a tool result
never appears in the database file.

## 5. Observability

Spans follow OTel `gen_ai.*` conventions so any backend works:

| Span | Key attributes |
|---|---|
| `invoke_agent rigging` | `gen_ai.agent.name`, `rigging.spec_hash`, `rigging.session_id` |
| `chat <model>` | `gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens`, `gen_ai.response.finish_reasons` |
| `execute_tool <name>` | `gen_ai.tool.name`, `rigging.tool.side_effecting`, `rigging.guardrail.verdict` |
| `guardrail <name>` | `rigging.guardrail.hook`, `rigging.guardrail.verdict`, `rigging.guardrail.confidence` |

Langfuse is the first OTLP target, MLflow the second. Both are `Exporter`
implementations; neither is imported by the loop.

## 6. Failure semantics

| Condition | Behaviour |
|---|---|
| transport error (429/5xx/timeout) | retry with capped exponential backoff, max 3, jitter |
| other 4xx | fail the run with the gateway's error body, as an `error` event |
| malformed SSE event | skip and record `sse_malformed` (never silently drop) |
| tool exception / bad args | tool message with `is_error: true` |
| guardrail deny | tool message (tool hook) or run ends with `GuardrailDenied` (input/output hook) |
| approval deny | tool message `denied by policy`; the model is told, the user is shown |
| limits reached | one final call with tools disabled; then `BudgetExhausted` with partial transcript |
| Ctrl-C | `CancelledError` propagates; partial transcript is already durable |
