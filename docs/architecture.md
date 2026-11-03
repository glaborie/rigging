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
| `Guardrail` | `on_input`, `on_tool_call`, `on_tool_result`, `on_output` → `Allow \| Deny \| Transform` | 4 built-ins | v0.3 |
| `ApprovalPolicy` | `request(call, tool, reason) -> (approved, note)` | CLI prompt, scripted (tests), web page (`--manifest`, ADR-0023) | v0.3 / v0.7 |
| `Retriever` | `search(query, k) -> list[Chunk]`, `requires_indexing` | Qdrant (default), FTS5 (reference) | v0.3 |
| `DecisionModel` | `decide(state, questions) -> typed answers + probabilities` | Laya (advisory) | v0.3 |
| `Exporter` | `span(name, attrs)`, `event(...)` with `gen_ai.*` attrs | OTel→Langfuse, MLflow (run-level), console | v0.4 / v0.6 |
| `SubagentRunner` | `spawn(spec, task) -> SubagentResult` | `spawn_<name>` tool over `run_agent` (ADR-0019) | v0.6 |

Every protocol is defined in `rigging/protocols.py`, one screen each, with no
imports from the rest of the package. That rule is what makes the swappability
real rather than aspirational: an implementation can be written and tested
without touching the loop.

## 3. The loop (v0.1 contract)

```
spec = load_manifest(path)            # JSON Schema validated
agent = assemble(spec, registry)      # names → implementations, fail fast on unknown
run  = eventlog.start_run(spec_hash)  # parent_run_id set for subagents (ADR-0019)

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
                               -- model_response | tool_batch | tool_call
                               -- approval_requested | approval_verdict | tool_result
                               -- budget_check | escalation | output_verdict
                               -- run_finished | error
                               -- eval_verdict (written by `harness eval`)
  payload     TEXT NOT NULL,   -- JSON, redacted before insert
  span_id     TEXT,
  parent_run_id TEXT,          -- set when this run was spawned as a subagent
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

The two backends see different shapes, on purpose (ADR-0020). Langfuse ingests
spans. MLflow tracks runs, so `MLflowExporter` maps one rigging run to one MLflow
run: the root span creates it, child spans are aggregated into metrics
(`gen_ai.usage.*`, `rigging.steps`, `rigging.chat_calls`, `rigging.tool_calls`,
`rigging.latency_s`, `rigging.cost_usd`), and the root span ends it `FINISHED` or
`FAILED`. It speaks MLflow's `2.0` tracking REST API over `httpx`, so there is no
`mlflow` dependency and no extra to install.

For that to be honest, the loop ends the root span on **every** exit path — `ok`,
`denied`, `budget_exhausted` and output-deny — with `rigging.status` set. A
run-level backend cannot infer a status it is never told.

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
## 8. The web view (v0.5 read-only, v0.7 chat; ADR-0018, ADR-0023)

`rigging/service/` is a projection, not a layer: every read is a query over
`sessions`, `runs` and `run_events`. Two modes share one page.

| Route | Shape |
|---|---|
| `GET /` | one HTML page, vanilla JS, no build step |
| `GET /api/config` | whether chat is on, and which agent/model it would run |
| `GET /api/runs?limit=` | recent runs joined to their session title |
| `GET /api/runs/{id}` | the run plus its events, in `seq` order |
| `GET /api/sessions/{id}` | every root run of a session with its events, oldest first |
| `GET /api/runs/{id}/stream` | SSE: replay, then tail by `seq` until the run ends, goes idle, or hits `timeout` |
| `POST /api/chat` | *(with `--manifest`)* start one `run_agent`, return `{session_id, run_id}` |
| `POST /api/runs/{id}/approval` | *(with `--manifest`)* answer a waiting side-effecting tool |

Properties that stay deliberate:

- **Reads never write.** The read path opens the log with SQLite `mode=ro`, so
  the read-only claim is enforced by the driver, not by discipline.
- **One loop.** The write path calls `run_agent` in a worker thread — the same
  function `harness run` calls — and returns before the turn finishes; the page
  follows the run through the same SSE route. There is no second loop and no
  second streaming implementation.
- **Approvals stay in the loop.** `WebApproval` is an `ApprovalPolicy` that blocks
  the loop thread until the page answers; the loop writes `approval_requested`
  and `approval_verdict` and makes the decision. No answer in ten minutes is a
  denial. The page cannot approve anything the loop did not ask about.
- **No second store.** The page renders whatever is in the log, and multi-turn
  history is `EventLog.session_messages()` — a projection over the session's root
  runs. A restart loses nothing.
- **No push infrastructure.** The SSE generator polls `run_events` every 300 ms
  by `seq`. A run that outlives `idle_seconds` closes the connection; the page
  does not auto-reconnect (it uses `fetch` + `ReadableStream` so it can send a
  bearer header), so the reader reopens the stream by picking the run again. That
  keeps a long agent run from pinning a worker forever.

The write path is opt-in: without `--manifest` the app has no POST route, which
is exactly ADR-0018. With it, chat requires `RIGGING_SERVICE_TOKEN` even on
loopback — any page the browser visits can POST to a local port — and a second
turn while a run is in flight is a `409` rather than two interleaved histories.

Auth (ADR-0021) is one bearer token, `RIGGING_SERVICE_TOKEN`, checked with
`hmac.compare_digest` on every `/api/*` route. `GET /` stays public: it is static
markup with no run data, and the page asks for the token when its first API call
returns 401, keeping it in `sessionStorage`. The stream is consumed with
`fetch` + `ReadableStream` rather than `EventSource`, because only the former can
send a header — the token never appears in a URL. With no token configured,
`harness serve` refuses any non-loopback bind instead of opening a port.

Every response carries a CSP with a per-response nonce (the page's own inline
script and style are the only code allowed to run), plus `nosniff`,
`no-referrer`, `X-Frame-Options: DENY` and `no-store`. `/api/*` is rate-limited
per client IP before the token check (240/min default, `429` + `Retry-After`).
The key is `request.client.host`; `harness serve` passes the trusted-proxy list
to uvicorn, so `X-Forwarded-For` is honoured only from a configured proxy and an
untrusted peer cannot choose its own bucket. The exposure model — TLS proxy,
loopback bind, the `sessionStorage` residual risk, and what is explicitly not
claimed — is [`threat-model.md`](threat-model.md).

The page is a real browser test, not just a route test:
`tests/test_service_browser.py` serves the app on a loopback socket, loads `/` in
headless Chromium, and asserts the run list renders with no page error
(`make test-browser`, `[browser]` extra, its own CI job). A second test drives
the write path: it types a prompt, clicks approve on the approval card, and
asserts the tool result appears. It exists because v0.6.0 shipped a page whose
inline JS failed to parse while `GET /` still answered 200 — see the postscript
in ADR-0018.

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

## 10. Parallel tool execution (v0.6, ADR-0022)

Off by default. `parallel_tools: true` in the manifest changes one thing: the
`tool.run` calls of a **single model response** run in a bounded thread pool
(8 workers). The step is three phases:

| Phase | Thread | Guarantee |
|---|---|---|
| prepare | main | `tool_call` event, span, `on_tool_call` guardrails, approval — all in declared order |
| execute | pool | only approved calls; an exception becomes an `is_error` result |
| finish | main | span end, `on_tool_result`, `tool_result` event, `tool` message — in declared order |

Approval is deliberately serial: a consequential call must be decided before any
tool starts, and two calls cannot race for one terminal prompt. The event log
stays single-writer, so `seq` remains contiguous and the message list keeps the
model's declared order even though completion order varies. A `tool_batch` event
records the mode and the call names for any multi-call response, so an eval can
tell a parallel batch from a serial one.
