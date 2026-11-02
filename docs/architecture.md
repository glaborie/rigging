# Architecture

> Status: v0.4 complete. The interfaces below exist in code; implementations are
> built and tested.

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
                    │  source 1: decorators in-process    source 2: entry points       │
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

Entry-points are discovered automatically during assembly; the list is logged to
stderr for visibility. To suppress, provide an explicit `Registry`.

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

## 7. Entry-points for third-party capabilities

Third-party packages can register capabilities via Python entry points. The supported
groups are:

- `rigging.tools` → registers a tool (decorate a callable with `@tool()`)
- `rigging.guardrails` → registers a guardrail (decorate a class with `@guardrail()`)
- `rigging.retrievers` → registers a retriever (decorate a class with `@retriever()`)
- `rigging.decision_models` → registers a decision model (decorate a class with `@decision_model()`)

A minimal `pyproject.toml`:

```toml
[project.entry-points."rigging.tools"]
my_tool = "my_package:register_tools"

[project.entry-points."rigging.guardrails"]
my_guard = "my_package:register_guardrails"
```

The registration function receives the `Registry` and can use its `add()` method:

```python
# my_package/__init__.py
from rigging import tool, guardrail, Registry

def register_tools(reg: Registry) -> None:
    @tool("my_tool", side_effecting=True, registry=reg)
    def my_tool(arg: str) -> str:
        return f"processed: {arg}"
```

Entry-points are discovered automatically during `assemble()` and logged to stderr.
To disable discovery, pass an explicit `Registry()` with `registry=registry` to
`assemble()` or `run_agent()`.
## 8. The web view (v0.5, ADR-0018)

`rigging/service/` is a projection, not a layer: it reads `sessions`, `runs` and
`run_events` and nothing else.

| Route | Shape |
|---|---|
| `GET /` | one HTML page, vanilla JS, no build step |
| `GET /api/runs?limit=` | recent runs joined to their session title |
| `GET /api/runs/{id}` | the run plus its events, in `seq` order |
| `GET /api/runs/{id}/stream` | SSE: replay, then tail by `seq` until the run ends, goes idle, or hits `timeout` |

Three properties are deliberate:

- **No writes.** The log is opened with SQLite `mode=ro`, so the read-only claim
  is enforced by the driver, not by discipline. There is no launch or approve
  endpoint: approvals stay in the loop where `ApprovalPolicy` lives.
- **No second store.** The page renders whatever is in the log, so it cannot
  drift from `harness runs` or the eval CLI.
- **No push infrastructure.** The SSE generator polls `run_events` every 300 ms
  by `seq`. A run that outlives `idle_seconds` closes the connection and the
  browser's `EventSource` reconnects, which keeps a long agent run from pinning
  a worker forever.

The cost is stated in the ADR: read-only means no interactivity, and loopback
with no auth means it is a development window, not a deployment.

## 9. Subagents (v0.6, ADR-0019)

A subagent is a child **run**, not a subprocess: same event log, same session,
`runs.parent_run_id` set, its own manifest (model, prompt, guardrails, budget).

| Piece | Where |
|---|---|
| declaration | `subagents: [{name, manifest, description}]` in the parent manifest |
| tool | `spawn_<name>(task)`, `side_effecting=True` |
| child run | `run_agent(child_spec, task, parent_run_id=…, depth=…, approval=parent.approval)` |
| cap | `RIGGING_SUBAGENT_MAX_DEPTH` (default 1), enforced at call time as an `is_error` result |

Four rules make the tree debuggable rather than magical:

- **The log is shared.** The child's events are in the same database, so
  `harness runs` and the web view show both, indented by `parent_run_id`.
- **History is not inherited.** The child receives the task string and its own
  system prompt; the parent's messages never cross the boundary.
- **Approvals are inherited, policies are not.** The child uses the parent's
  approval *object*, so a manifest cannot promote itself to `scripted`.
- **Usage is per run.** The child's tokens appear in its own run row and in the
  tool result; they are not repriced at the parent's model and not charged to the
  parent's token budget. The parent's step budget bounds how often it can spawn.

A child that ends `denied`, `budget_exhausted` or `error` still returns a JSON
summary — with `is_error` set, so the parent model sees the failure and can react.
