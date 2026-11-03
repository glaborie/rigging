# Milestones

Each milestone is shippable on its own. The go-public gate is deliberately early:
the first point at which a stranger can run it and understand it.

**Current state (2026-11-03):** v0.1–v0.5 are shipped and v0.6 (scale-out) is
released as v0.6.3 (v0.6.0 was tagged, then patched for doc drift and the page
bug in v0.6.1, hardened in v0.6.2, and given an explicit proxy-trust model in
v0.6.3 — see the v0.6 sections). v0.5
delivered the manifest schema, entry-points discovery, the
architecture/ADR pass, the README recording and the read-only web widget; v0.6
delivered subagents, the MLflow exporter, service auth and opt-in parallel tool
execution. 158 offline tests plus a browser smoke test; ruff + `mypy --strict`
clean. The four v0.6 proofs are in [`showcase-v0.6.md`](showcase-v0.6.md).

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
- [x] `protocols.py`: LLMClient, Tool, Guardrail, ApprovalPolicy, Retriever, DecisionModel, Exporter, SubagentRunner
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

- [x] `SubagentRunner`: child runs with own manifest/budget, `parent_run_id` wired
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
      say where the trust list is configured. `make check`: 158 passed; the
      browser test still renders under the CSP.

## Standing rules

1. The demo must pass its eval suite on the **weakest documented model** (`qwen3:8b`).
2. No feature ships without an event type and a test that asserts on it.
3. Every new abstraction needs an ADR or it is not merged.
