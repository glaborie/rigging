# ADR-0025: PII on the model path uses reversible sentinels, scoped to one run

**Status:** Accepted (v0.8)

> **Scope amended by ADR-0026.** The map now lives for a session rather than a
> single run; this page is kept for the reasoning about reversible placeholders
> themselves.

> The parked `spike/gheim-pii-detector` branch also uses number 0025. If that
> branch is ever revived it needs renumbering; this one is on `main`.

## Context

`pii_redaction` replaces every detected span with `[REDACTED]` and that is the
whole value. It protects the transcript and the provider, and it costs the agent
its ability to act: with `[REDACTED]` in the conversation, a support agent cannot
tell one customer's address from another's, cannot pass an address to
`notify_customer`, and cannot say "I emailed the address on file". The model is
reduced to asking the user to repeat a value it was just given.

The identifier also has two different audiences. The **provider** should never
see it. The **tool** frequently must — that is the point of the tool. A single
substitution cannot serve both, so the value needs to be replaced with something
that is stable, readable, and reversible *inside the process only*.

## Decision

**A second guardrail, `pii_pseudonymise`, replaces PII with stable sentinels
(`[EMAIL_1]`) and restores the real values in tool arguments.** The mapping is a
`SentinelMap` created per run, held in memory, and never written to the log.

The loop already runs its hooks in exactly the order this needs, so `loop.py`
does not change:

| hook | action | why the order is safe |
|---|---|---|
| `on_input` | value → sentinel | runs before `input_received` is logged |
| `on_tool_call` | sentinel → value | `tool_call` is logged *first*, so the log keeps the sentinel |
| `on_tool_result` | value → sentinel | runs before `tool_result` is logged (`_finish_call`) |
| `on_output` | sentinel → value, **only if `restore_output=True`** | see below |

Sentinel format is `[EMAIL_1]`, not gheim's `<PERSON_1>`: the web view renders
markdown, where `<...>` is swallowed as an unknown HTML tag, so a `<EMAIL_1>` in
a tool card would be invisible.

The map is bidirectional and stable. If `lookup_order` returns an address that
`on_input` already replaced, it reuses `[EMAIL_1]` instead of minting
`[EMAIL_2]`. A sentinel the map never issued is left alone by `deanonymise`, and
`on_tool_call` **denies** the call rather than forwarding the literal: the map is
per run, so a placeholder echoed from an earlier turn cannot be resolved, and
passing `[EMAIL_1]` on would write it into whatever the tool persists. The model
sees the denial as a tool error and can ask for the value.

`restore_output` defaults to **false**, so the user sees `[EMAIL_1]` in the final
answer. Restoring it is one flag, but `on_output` runs *before* the root span is
built, and spans are not redacted (`redact_event` guards SQLite only), so
restoring there writes the real value into the trace. The default keeps the
whole surface clean; the flag exists for deployments that have turned
`observability.capture_content` off.

`pii_redaction` is unchanged and still available. `redact_event` at the log
boundary is unchanged: it stays a stateless pattern floor, because a boundary
must not depend on a map that may not exist.

`assemble()` now instantiates each guardrail **once per manifest** and shares it
across hooks. Before this ADR it built one instance per `(hook, name)` pair, so
listing `pii_pseudonymise` on four hooks would have produced four maps and four
independent lifetimes. Two different `config` blocks for one guardrail name are
now a manifest error rather than a silent last-one-wins.

## Consequences

- **The log is no longer irreversibly redacted.** This is the real cost. A leaked
  `rigging.db` used to be a dead end; now it is a substitution cipher whose key
  lives in process memory. The key is never persisted, so the log alone is still
  not invertible — but this is strictly weaker than `[REDACTED]` and the threat
  model now says so. The map is a secret on the same footing as the bearer token.
- **Spans are not redacted.** `redact_event` protects SQLite, not the exporter.
  That is true today and this ADR does not change it, but it decides
  `restore_output`'s default. A deployment that turns this on should turn
  `capture_content` off.
- **Scope is one run.** The web chat rebuilds history from the log each turn, so
  turn 2 sees `[EMAIL_1]` with no map and a tool gets a literal. Subagents are
  worse: a child `run_agent` assembles its own guardrails and starts empty. Both
  are documented limits, not bugs to discover. A session-scoped store with a TTL
  is the obvious follow-up and needs its own ADR.
- **Two PII guardrails exist.** They are alternatives, not a stack: running both
  means the redactor runs second and destroys the sentinel it just received.
  Manifests choose one.
- **Guardrail instances are shared across hooks.** A third-party guardrail that
  was relying on being constructed once per hook (and therefore on having
  per-hook state) now sees one instance. Nothing built in relied on it; a
  stateless guardrail cannot tell the difference.
- **Collision.** Text that literally contains `[EMAIL_1]` is indistinguishable
  from a sentinel. Acceptable for a support transcript; a nonce prefix would fix
  it at the cost of readability.
- **A prompt must not print a concrete sentinel.** An early version of the demo
  prompt showed `[EMAIL_1]` as an example and the model copied it into
  `submit_basket` on a turn where no address had been given, writing the literal
  into the order. The denial above is the safety net; the prompt now describes
  the placeholder without printing one.
- **Approval is on the proposal, not the execution.** `tool_call` and
  `approval_requested` record the model's arguments, so an approver sees
  `[EMAIL_1]`, not the address the tool will use. Logging the restored value
  would put it in the log, which is the one place it must not be. A deployment
  that needs value-level approval has to surface it out of band.
- **No dependency.** Unlike the parked gheim spike, this is the existing pattern
  list plus a dict.

## Alternatives rejected

**`<PERSON_1>` (gheim's format).** Invisible in this repo's markdown view.

**Persist the map beside the log.** Makes transcripts re-identifiable by anyone
with the database — the exact property the log boundary exists to deny.

**Scope the map to the session now.** Correct for multi-turn chat, but it needs a
store keyed by `session_id`, a TTL, and a seam to inject it into a guardrail that
`assemble()` builds per run. That is a second design; this one is complete
without it.

**Restore the output by default.** Puts the real value in the root span for every
deployment, silently.

**Replace `redact_event` with the map.** The boundary would then depend on
per-run state: a run that never built a map would log raw PII. The floor stays
stateless.

**Let the model call a tool to resolve a sentinel.** A round trip per lookup, and
the model would have to be trusted to use it. Substitution at the boundary is
free and unconditional.
