# ADR-0026: Sentinel maps live for a session, not a run

**Status:** Accepted (v0.8)

**Supersedes:** the one-run scope in ADR-0025.

## Context

ADR-0025 scoped the `[EMAIL_1]` map to a single run, because a run is the unit
the loop already assembles. That is one turn too short, and the demo proved it in
the most ordinary flow there is:

```
turn 1  "my email is anna@…"      → [EMAIL_1], map discarded at the end of the run
turn 2  "confirmé"                → map is empty
        submit_basket(contact=?)  → nothing to restore
```

The model, having seen `[EMAIL_1]` in the history it was given, passed that
literal back. A guardrail now denies an unresolvable placeholder, so the order is
not written with `[EMAIL_1]` in its contact column — but the flow is still broken:
a customer who gives an address and confirms in two turns cannot be served.

The map is not run state. It is conversation state, and the loop already has a
name for that boundary: the session.

## Decision

**A `SentinelStore` holds one `SentinelMap` per session, in process memory.**

- Keyed by `session_id`; bounded by a TTL (3600 s) and a session cap (256), both
  pruned on access. Guarded by a `threading.Lock`, because the web service runs
  turns from several sessions in worker threads.
- Never written to the log, the exports, or disk. It is the same secret ADR-0025
  described, with a longer life.
- `assemble()` takes `session_id` and passes it to any guardrail that opts in
  with `wants_session = True`. `PIIPseudonymiseGuardrail` does; it resolves its
  map from the store at construction instead of allocating one.
- `run_agent` forwards the session it was given. The web chat, the REPL and the
  `spawn_*` tools all pass one, so a subagent inherits the parent's map and can
  resolve what the parent sent it.
- A caller that passes no session — `harness run` without `--session`, an eval
  case — keeps the ADR-0025 behaviour: a map that dies with the run. Eval cases
  must not share sentinels, so that is correct there rather than a fallback.
- An unresolved placeholder is still a `Deny` at `on_tool_call`. The store
  widens what resolves; it does not make an unknown sentinel acceptable.

## Consequences

- **Memory is bounded and the key is still not persisted.** TTL expiry and the
  session cap bound the store; a restart forgets every mapping. A transcript
  whose session expired can no longer be re-identified through it, and one whose
  session is live can.
- **The blast radius grows from one run to one session.** A map that leaked
  before covered a turn; it now covers up to an hour of a conversation. The
  threat model's T15 is updated rather than left as "one run".
- **One process.** Two workers behind a load balancer would each hold their own
  maps, and a session routed to a different worker would see its sentinels
  denied. That is the same constraint the rate limiter already documents; the
  documented deployment is one process behind a proxy.
- **A session id is now a key into secret state.** Nothing exposes the map, and
  the id is a random `s_…`, but two agents sharing a session id would share
  sentinels. The service ties a session to one manifest.
- **`assembled` guardrails gain an optional constructor argument.** It is
  keyword-only and opt-in via a class attribute, so the four other built-ins and
  every third-party guardrail are unaffected.
- **The denial remains the safety net** for a placeholder from a pruned session,
  a restarted process, or a model that copies one out of the prompt.

## Alternatives rejected

**Pass a `RunContext` to all four guardrail hooks.** Uniform, and the shape
ADR-0024 gave tools — but it is a breaking change to every guardrail and to
every test that calls a hook directly, for one need. Revisit if a second
guardrail wants run identity.

**Persist the map beside the log.** Makes the transcript re-identifiable by
anyone holding the database, which is the property the log boundary denies.

**One global map with no session key.** Simplest, and leaks one conversation's
sentinel into another's: two customers both become `[EMAIL_1]`.

**Ask the customer to repeat the address every turn.** That is what the denial
leaves you with, and it is not a support agent.

**Keep it per run and document the limit.** ADR-0025 did exactly that; the limit
turned out to be the normal case.
