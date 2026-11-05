# Milestones

Each milestone is shippable on its own. The go-public gate is deliberately early:
the first point at which a stranger can run it and understand it.

**Current state (2026-11-03):** v0.1–v0.6 are shipped (v0.6.3 was the last patch:
v0.6.0 was tagged, then patched for doc drift and the page bug in v0.6.1,
hardened in v0.6.2, and given an explicit proxy-trust model in v0.6.3). v0.5
delivered the manifest schema, entry-points discovery, the architecture/ADR pass,
the README recording and the read-only web widget; v0.6 delivered subagents, the
MLflow exporter, service auth and opt-in parallel tool execution. v0.7.0 makes
the web view drivable: chat and in-page approvals, opt-in and token-gated
(ADR-0023). 204 offline tests plus two browser tests; ruff + `mypy --strict`
clean. The demos are [`showcase-v0.7.md`](showcase-v0.7.md) (browser) and
[`showcase-v0.6.md`](showcase-v0.6.md) (four commands).

**Post-v0.4 hardening (2026-11-02)** — not a milestone, but it changed how the
repo is operated, so it is recorded here:

- **Langfuse Cloud replaces the self-hosted stack.** The `observability` compose
  profile (Langfuse v3 + Postgres + ClickHouse + Redis) never started cleanly and
  was dropped. `docker compose` now runs `omniroute` and, under `--profile
  retrieval`, `qdrant`. Traces go to `cloud.langfuse.com`.
- **`.env` is authoritative.** `LANGFUSE_HOST/_PUBLIC_KEY/_SECRET_KEY` and the
  `RIGGING_PROFILE_<NAME>_*` credential profiles are read through `Settings`
  (`extra="allow"`), so `harness run` needs no exported shell variables; the real
  environment still wins over `.env`.
- **`credential_profile` is honoured.** The loop had been building its client from
  the default gateway and ignoring the manifest's profile.
- **Spans flush on every exit path.** Exporters close on `ok`, `denied`,
  `budget_exhausted` and output-deny; previously only the ok path closed them, so
  other runs buffered their spans and exited silently.
- **`make trace` is the proof.** `make check` and `make observability` emit no
  spans by design; `make trace` runs the example and polls
  `GET /api/public/v2/observations` until they land.

## v0.1 — the loop is real

- [x] `types.py`: Message, ToolCall, ToolResult, Usage, ModelResponse (with `reasoning`)
- [x] `protocols.py`: LLMClient, Tool, Guardrail, ApprovalPolicy, Retriever, DecisionModel, Exporter
- [x] `registry.py`: decorators + `discover_entry_points()` (opt-in)
- [x] `client/openai_compat.py`: httpx, SSE parser, retries, `usage` mapping, reasoning passthrough
- [x] `loop.py`: the sequence in `docs/architecture.md` §3, tool errors as messages
- [x] `events.py`: SQLite schema, append-only writer, redactor boundary
- [x] `manifest.py`: load + JSON Schema validation + defaults merge
- [x] `cli.py`: `harness run`, `harness schema`; streaming to terminal
- [x] tests: SSE wire fixtures (chunk reassembly, tool-call deltas, malformed, 429), loop unit tests with a fake client

**Exit criteria — v0.1 DONE (2026-10-29):** live runs through the full stack
(harness loop -> OmniRoute -> freellmapi -> codestral, and `beam:cloud` via ollama
cloud) completed end-to-end with the full event trail in SQLite; the **two-tool
task** (`examples/two-tools/`, `current_time` + `sha256_hex`) completed in 4 steps
with both tools called and a verifiable digest. Transcript:
`examples/hello/transcript-codestral.jsonl`; malformed-SSE covered by
`tests/test_client.py`; core tools covered by `tests/test_core_tools.py`.
Remaining nice-to-have, not gating: a run against a pulled `qwen3:8b`
(`ollama pull qwen3:8b`).

## v0.2 — knowledge and memory ⇒ GO-PUBLIC GATE MET (repository remains private)

- [x] `skills.py`: filesystem discovery, frontmatter parsing, index-only system prompt
- [x] `tools/builtin.py`: `load_skill`, `escalate_to_human`
- [x] sessions table, `harness chat` REPL (`/transcript`, `/escalate`; per-turn runs share one session). Verified live: two-turn run through the gateway recalled a codeword given in the first turn
- [x] JSONL export + `harness run --replay`. Verified live: a recorded two-tool run replayed byte-identical with the API base at a dead port (tests: `tests/test_replay.py`)
- [x] README quickstart verified from a clean clone on Linux (2026-10-29: fresh clone, install 1 s, 34 tests green, live run in 3 s)

**Publish gate — met except the flip (2026-10-29):** 5-minute zero-API-key quickstart ✓
(verified from a clean clone) · green CI on `main` ✓ · committed demo transcript ✓ ·
architecture diagram ✓ · no TODOs in user docs ✓ · tagged `v0.2.0` with release notes ✓.
The repo stays **private** for now by decision; flipping it public is the remaining step
(then add branch protection, which needs a public repo or Pro).

## v0.3 — safety and the demo

- [x] four guardrails: PII redaction, prompt-injection heuristic, consequential-action
      gate, out-of-policy-commitment check (all wired into the demo manifest)
- [x] `ApprovalPolicy`: CLI prompt, scripted, auto-deny
- [x] `retriever/qdrant.py` (ollama `nomic-embed-text`, behind `[qdrant]`) + `retriever/fts5.py`
      (reference, zero-credential); `harness index` (no-op for fts5, embeds+upserts for qdrant)
- [x] `decide/laya.py` behind `[laya]`, advisory only
- [x] `examples/b2b-support/`: seeded SQLite fixture DB (40 products, 12 orders, 19 policy
      chunks — deterministic seed=7), 2 skills, 5 tools, agent.yaml
- [x] `make run MANIFEST=examples/b2b-support/agent.yaml ...` — zero credentials: fts5
      retriever over the seeded demo.db, model via the local OmniRoute stack

**Exit criteria — MET, verified live 2026-10-29 (beam:cloud via OmniRoute):**
1. *Refund blocked pending approval:* refund_order(112.50, ORD-1002) →
   `approval_requested` → operator answered "n" → `approval_verdict: approved=false`
   → tool denied → model loaded the `escalation-summary` skill → structured handoff.
2. *Over-authority caught:* a $1,350 refund attempt was denied by the
   consequential-action guardrail ($500 limit) before execution → escalated with reason
   `needs_approval`. (Bonus honesty: a deliberately wrong amount, $186.40, was caught
   by the model reconciling against lookup_order and escalated instead of processed.)
3. *Out-of-policy commitment:* permanent-discount demand refused with the CMP-001
   passage cited, then escalated.
4. *Human demanded:* explicit "get me a human manager" → structured handoff with
   verified order facts (reason=customer_requested).

## v0.4 — observability and evaluation

- [x] `Exporter` + OTel `gen_ai.*` spans, OTLP → Langfuse behind `[langfuse]`; live nested trace verified
- [x] `harness eval <suite.yaml>` over event-log predicates; cost/latency/pass table
- [x] decision dataset (`tests/data/decisions.jsonl`, 42 labelled cases) + `rigging/decide/calibrate.py` scorer + `harness calibrate` CLI; deterministic safety checks stay authoritative. Live scoring of Clef-Flash pending the Cloudflare quota reset
- [x] FreeLLMAPI adapter for `@cf/cloudflare/clef-flash` via `/v1/decisions`; contract tests pass. Live request reached Cloudflare but returned HTTP 429 (daily free quota exhausted), so successful live classification is pending
- [x] OpenRouter adapter for `respan/span-01-lite:free` via `/api/v1/systemone`; Span-01 accepts only `noul` questions, so choice questions fan out to one noul per option and renormalise. **Live calibration: n=42, accuracy=0.833, Brier=0.269**
- [x] Ollama SystemOne adapter for `d1-omni` (local, no key). **Live calibration on the same 42 cases: accuracy=0.548, Brier=0.479**
- [x] MCP tool source behind `[mcp]`, namespaced tools; live stdio echo-server round trip verified (mcp>=2.0)
- [x] 6–8 golden eval cases for the demo agent; live B2B suite passed 8/8

**v0.4 status: DONE (2026-10-29).** All boxes ticked. Both registered models were
scored on the same 42-case dataset — `respan/span-01-lite:free` on OpenRouter
(0.833 / 0.269) and local `d1-omni` (0.548 / 0.479); README has the side-by-side
table. `@cf/cloudflare/clef-flash` still waits on its daily cloud quota for the
same run. Version bumped to 0.4.0.

## v0.5 — the professional finish

- [x] manifest JSON Schema published + `harness schema` documented
- [x] entry-points discovery on by default, with a documented third-party example
- [x] `docs/architecture.md` final, ADRs complete, writeup published
- [x] 60-second terminal recording in the README
      `scripts/demo.sh` is the script; `scripts/record_demo.py` records it in a
      real pty to `docs/demo.cast` (asciinema v2, replayable), and
      `scripts/cast_to_gif.py` renders `docs/demo.gif` with ffmpeg. `make record`
      regenerates both. The take is live: host Ollama, seeded demo database, a
      scratch event log — tools, a guarded refund run, then the event trail.
- [x] stretch: FastAPI + SSE web widget reading the same SQLite sessions
      `rigging/service/` behind `[service]`, ADR-0018: read-only by construction
      (SQLite `mode=ro`), no second store, SSE tailing `run_events` by `seq`,
      one HTML page with no build step, loopback only. `make serve`; 8 tests
      cover the routes, the SSE replay, the idle cutoff, 404s and the no-write
      guarantee.

**v0.5 status: DONE (2026-11-02).** All five boxes ticked. Version 0.5.0.

## v0.6 — scale-out (optional) — DONE (2026-11-02)

All four items shipped: subagents (ADR-0019), MLflow exporter (ADR-0020), service
auth (ADR-0021), parallel tool execution (ADR-0022). **Released as v0.6.0
(2026-11-03); v0.6.1 aligned the docs and added the browser test; v0.6.2
hardened the exposure (TLS, rate limit, CSP, threat model).** Run [`showcase-v0.6.md`](showcase-v0.6.md) for the
four commands and the evidence each one leaves.

- [x] subagent tools: child runs with own manifest/budget, `parent_run_id` wired
      ADR-0019. `subagents:` in the manifest becomes `spawn_<name>(task)`,
      side-effecting so the approval gate applies; the child is a run in the same
      log and session, with its own model, prompt and limits, isolated history,
      and `runs.parent_run_id` set. Depth capped by
      `RIGGING_SUBAGENT_MAX_DEPTH` (default 1) as a structured `is_error` result.
      `examples/subagents/` runs live on `qwen3:8b`; 8 tests in
      `tests/test_subagents.py` (child in the same log, failed child, auto-deny,
      depth cap, usage accounting, missing manifest, name collision, redaction).
- [x] FastAPI service + auth (bearer keys or OIDC) designed as its own ADR
      ADR-0021: bearer tokens only for now, OIDC deferred with a reason.
      `RIGGING_SERVICE_TOKEN` (comma-separated for rotation) guards every
      `/api/*` route with `hmac.compare_digest`; `GET /` stays public so the page
      can prompt for the token, which it keeps in `sessionStorage`. The stream
      moved from `EventSource` to `fetch` + `ReadableStream` so the header can be
      sent and the token never lands in a URL. No token means loopback only, and
      `harness serve` refuses `0.0.0.0` with an explicit message. Verified live:
      page 200, `/api/runs` 401 without the token, 200 with it, 401 with a wrong
      one; `--host 0.0.0.0` without a token exits 2. 7 route tests. The page was
      served but not *rendered* until 2026-11-03: a Python `\n` had put a real
      newline inside the inline JS, the script failed to parse, and the page sat
      on "loading runs…". `INDEX_HTML` is now a raw string, a failed load says so,
      and `tests/test_service_browser.py` loads the page in headless Chromium and
      asserts the run list renders (`make test-browser`, its own CI job).
- [x] MLflow exporter as the second `Exporter`
      ADR-0020. One rigging run per MLflow run over the `2.0` tracking REST API
      (`runs/create`, `runs/log-batch`, `runs/update`), no SDK and no extra:
      root span creates and finishes the run, child spans become metrics.
      Required a loop fix: the root span is now ended on every exit path, so a
      denied or exhausted run is `FAILED` rather than stuck `RUNNING`.
      Verified live against `mlflow server` (SQLite backend): run `FINISHED`,
      tags/params/metrics matching the harness run; 6 tests in
      `tests/test_mlflow.py` use `httpx.MockTransport` (no server, no network).
- [x] parallel tool execution behind an opt-in flag
      ADR-0022: `parallel_tools: true` runs only `tool.run` for one model
      response in a bounded pool (8); prepare (events, spans, `on_tool_call`,
      approval) and finish (spans, `on_tool_result`, events, messages) stay
      serial and in declared order, so approvals cannot race and `seq` stays
      contiguous. A raising third-party tool becomes an `is_error` result. A
      `tool_batch` event records the mode for any multi-call response, so an eval
      can tell a parallel batch from a serial one. Verified live on `qwen3:8b`: one
      response with two calls produced `tool_batch {"parallel": true}` and both
      results. 9 tests (overlap, sequential default, serial-mode event, single
      call is not a batch, order, approval-before-run, auto-deny isolation, raising
      tool, guardrail deny).

## v0.6.2 — security hardening (2026-11-03)

Patch on v0.6.1 from a security review of the web view. No protocol, loop or
manifest changes.

- [x] **TLS is required to expose the service.** A non-loopback bind with a
      bearer token is refused unless the socket has TLS (`--ssl-certfile`,
      `--ssl-keyfile`) or `RIGGING_SERVICE_ALLOW_PLAINTEXT=1` records that the
      network already protects the link. The supported deployment is a TLS
      reverse proxy with uvicorn on loopback (ADR-0021 postscript).
- [x] **Per-client rate limiting on `/api/*`.** Fixed-window, 240/min default
      (`RIGGING_SERVICE_RATE_LIMIT`, `0` disables), checked before the token so a
      guessing script spends its own budget; `429` + `Retry-After`. Memory is
      bounded when keys are attacker-chosen. A backstop, not the proxy's limiter.
- [x] **CSP and security headers.** Per-response nonce for the page's inline
      script and style; `default-src 'none'`, `connect-src 'self'`,
      `frame-ancestors 'none'`; plus `nosniff`, `no-referrer`,
      `X-Frame-Options: DENY`, `no-store`. The browser test fails on a CSP
      violation, not just on a blank page.
- [x] **`docs/threat-model.md`.** Assets, actors, 13 threats with controls and
      residual risk, the TLS/proxy baseline, and the explicit non-claims
      (encryption at rest, per-user audit, token expiry, HA). The
      `sessionStorage` XSS exposure is stated there and in the README.
- [x] 11 new tests: limiter windows/keys/memory, headers and nonce, 429 before
      auth, disabled limiter, bind rules. `make check` 150 passed; the browser
      test still renders the page under the CSP.

## v0.6.3 — proxy-aware rate limiting (2026-11-03)

Patch on v0.6.2. The limiter keyed on `request.client.host` and relied on
uvicorn's implicit `X-Forwarded-For` default; behind a proxy the trust decision
was invisible, and a proxy on another host would have collapsed every client
into one bucket.

- [x] **The trusted-proxy list is explicit.** `RIGGING_SERVICE_TRUSTED_PROXIES`
      (default `127.0.0.1,::1`) is passed to uvicorn's `forwarded_allow_ips`;
      `*` and `/0` networks are refused at startup. An untrusted peer's
      `X-Forwarded-For` is ignored, so it cannot choose its own bucket, and a
      trusted proxy's header gives one bucket per client.
- [x] **Tested against a real server.** `tests/test_service_proxy.py` runs
      uvicorn and asserts per-client buckets from a trusted peer and a shared
      bucket from an untrusted one. The threat model documents the derivation
      table and the shared-bucket availability risk; the README and ADR-0021
      say where the trust list is configured. `make check`: 188 passed; the
      browser test still renders under the CSP.

## v0.7.0 — the demo you can drive (2026-11-03)

The web view could only read. A portfolio demo needs a reviewer to type a prompt
and watch the harness react, so the page gained a write path — opt-in, token-gated
and still run by the loop (ADR-0023, which supersedes the write-capable clause of
ADR-0017 and the no-write clause of ADR-0018).

- [x] **`harness serve --manifest` starts a run from the page.** `POST /api/chat`
      calls the same `run_agent` as the CLI in a worker thread and returns
      `{session_id, run_id}`; the page follows the existing SSE route. Without
      the flag the app has no write route at all.
- [x] **Approvals answered in the page.** `WebApproval` blocks the loop thread on
      a `threading.Event` until `POST /api/runs/{id}/approval` answers; the loop
      still writes `approval_requested`/`approval_verdict` and still decides. No
      answer in ten minutes is a denial. A second turn while a run is in flight
      is a 409, so histories cannot interleave.
- [x] **Multi-turn history is a projection.** `EventLog.session_messages()` joins
      the session's root runs in order; child runs are excluded. A restart loses
      nothing and a second tab sees the same conversation.
- [x] **Named runs.** `run_agent(run_id=...)` and an idempotent
      `EventLog.start_run` let the page subscribe before the first event. `append`
      now seeds its seq cursor from the log, so a run can be written by more than
      one `EventLog` (the web path needs exactly that).
- [x] **A demo transcript, not a log dump.** User/agent bubbles, one card per tool
      call with arguments and result, compact guardrail lines, an approval card,
      and a footer with model turns and tokens. `raw events` shows the unfiltered
      log. Opening a turn preloads the session's earlier turns.
- [x] **Writes need a token even on loopback.** `create_app` refuses a
      chat-enabled app without `RIGGING_SERVICE_TOKEN`; `harness serve --manifest`
      refuses to start. A page on any origin can POST to a loopback port.
- [x] **Tested in a browser, not only over HTTP.** The new browser test types a
      prompt, clicks approve, and asserts the tool result renders — the ADR-0018
      postscript rule applied to the write path.
- [x] 15 new tests (13 service + 3 events, plus the browser test).
      `make check`: 204 passed; `make test-browser`: 2 passed. The demo script is
      [`showcase-v0.7.md`](showcase-v0.7.md).

## v0.8 — the harness under attack

The eval suite asks whether the agent behaves on the cases we thought of. This
milestone is the other question, and it is the one a security reviewer will ask:
what happens when someone tries to make it misbehave? Not a new harness layer —
a probe suite pointed at the demo, with the judgement split between a
deterministic assertion on the run and a model grader on the reply.

- [x] **`redteam/provider.py`: a promptfoo target that reports facts.** One turn
      through `run_agent`, then `attempted` / `executed` / `denied` /
      `approval_denials` / `guardrail_denials` folded from the event log, plus a
      before/after diff of the fixture database. `tool_call` is an attempt and
      `tool_result` is what happened — the distinction the eval suite learned the
      hard way.
- [x] **Three approval postures**, because a scan needs to say three different
      things: deny everything (the unattended default), approve everything, or
      approve one named tool so the guardrails rather than the approval channel
      are on trial.
- [x] **Controls before scores.** Four known-good cases, one of which really
      executes an in-window refund and asserts the row changed; without it a
      target that refuses everything reports 0% attack success and means nothing.
- [x] **Two scans** (18 probes unattended, 15 with refunds pre-approved): no
      probe produced a side effect, no probe got a contact address out. The one
      failure was the grader's JSON parse, not the target — recorded, not
      suppressed, because that is the argument for deterministic predicates
      (ADR-0009).
- [x] **Scoped honestly.** `bola`/`rbac`/`bfla` are excluded because the demo has
      no caller identity to break, and `rag-poisoning` because the fixture has no
      poisoned documents in it. Both exclusions, and the follow-up commands, are
      in [`red-team.md`](red-team.md).
- [x] **One Langfuse trace per probe.** `redteam/run.sh` sets
      `REDTEAM_EXPORT=langfuse` when `LANGFUSE_*` is configured and the provider
      overlays the manifest's exporters, so a scan's trajectories land in the
      same backend as `make trace`: root `invoke_agent`, `chat` generations,
      `execute_tool` spans with arguments, results and `rigging.tool.denied`.
      Traces are named `redteam <plugin>#<n>` — Langfuse otherwise names them
      after the root span, and 33 rows called `invoke_agent` is not a scan — and
      tagged `probe:`, `strategy:`, `approval:`. `RIGGING_TRACE_NAME` is the
      settings field that carries the name; a missing `[langfuse]` extra fails
      the scan instead of exporting nothing silently (ADR-0012's cost).
- [x] 17 offline tests on the provider's pure parts; the scans themselves are
      opt-in and live. Generated probes are committed; the run logs are not.

## v0.8 backlog

- [ ] **LibreChat as a front end** (`http://localhost:3080`). Blocked on a
      decision, not on code: ADR-0023 rejects OpenWebUI/AnythingLLM/LibreChat by
      name — "rigging is not a server, and approvals need a channel those clients
      do not have". Serving `/v1/chat/completions` would reverse that, so it needs
      its own ADR first. What changed since 0023 was written: LibreChat's
      `shared_endpoint_settings` can send `{{LIBRECHAT_BODY_CONVERSATIONID}}`,
      which is the session key the sentinel vault (ADR-0027) needs to resolve
      `[EMAIL_1]` on the next turn. What has not changed: there is still no
      approval channel, so `refund_order` / `submit_basket` / `notify_customer`
      would answer "denied by approval policy" in that UI.

## Standing rules

1. The demo must pass its eval suite on the **weakest documented model** (`qwen3:8b`).
2. No feature ships without an event type and a test that asserts on it.
3. Every new abstraction needs an ADR or it is not merged.
