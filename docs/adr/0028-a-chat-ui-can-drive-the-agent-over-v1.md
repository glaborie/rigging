# ADR-0028: A chat UI can drive the agent through an OpenAI-compatible `/v1` surface

**Status:** Accepted (v0.8). Reverses the "OpenWebUI / AnythingLLM / LibreChat"
clause of ADR-0023's rejected alternatives. Amends ADR-0021 (what the token
protects) and ADR-0025/ADR-0027 (which session the sentinel map belongs to).

## Context

ADR-0023 rejected wiring an OpenAI-compatible client to the harness, and the
reason was sound: "rigging is not a server, and approvals need a channel those
clients do not have. Wiring them up would demo a model, not the harness." It built
a page instead, with an approval card, and that is still the demo.

Two things changed. First, the complaint did not go away — the first thing a
reviewer asks after `make web` is whether it works in the chat UI they already
run. Second, one concrete blocker was removed: LibreChat's custom endpoints can
send templated headers, including `{{LIBRECHAT_BODY_CONVERSATIONID}}`. Before
that, an OpenAI-shaped surface was stateless per request while the sentinel map
(ADR-0025, ADR-0027) is per session, so turn 2 would send `[EMAIL_1]` in the
history with no map to resolve it and a tool would store the placeholder. With a
conversation id on the request, the session — and therefore the vault — survives.

The second half of ADR-0023's objection stands and is not worked around: these
clients have **no approval channel**. There is no place to render
`approval_requested`, and no POST that answers it.

## Decision

Serve `GET /v1/models` and `POST /v1/chat/completions` from the existing service,
opt-in, for exactly one agent.

- **Opt-in and token-gated.** `harness serve --manifest … --openai-api`. The app
  refuses to build a bridge without `RIGGING_SERVICE_TOKEN`, for the same reason
  the chat write path does: a chat-completions endpoint is the shape every client
  already knows how to call, so an open one is an open agent.
- **One loop.** A request becomes `run_agent(...)`, exactly as `harness run` and
  the page do. The bridge translates the wire format; it does not reimplement
  tool-call assembly, guardrails, budgets or events. Streaming is
  `run_agent(stream=callback)` through a queue when the manifest has no output
  guardrail, and a buffered answer otherwise — neither is a second loop.
- **The session comes from the client, and is required.** `X-Conversation-Id` is
  hashed into a harness session id (`lc_<hash>`). Missing header → 400 with the
  `librechat.yaml` line that fixes it. The alternatives are worse: a new session
  per request silently loses the sentinel map, and one shared session resolves a
  placeholder from one conversation inside another.
- **No approvals: the loop denies.** The bridge replaces only the manifest's
  `cli` policy with `AutoDenyApproval`: its prompt would block the request thread
  on a terminal nobody can see. `auto_deny` and `scripted` are deliberate
  unattended choices and are left alone, the same rule the page follows
  (ADR-0023). A side-effecting tool is attempted, refused by the loop, recorded
  as `approval_verdict {approved: false}`, and the answer says so.
- **The manifest owns the system prompt.** Client-sent `system` messages are
  dropped, so a UI's persona cannot displace the guardrail instructions, and a
  client-sent `tool` message is dropped too — this server never emits a tool call
  to the client, so there is no id to attach one to. The client's prior turns are
  cleaned before the model sees them: the client stores the user's raw words, and
  replaying a raw address would undo the previous turn's pseudonyms. A manifest
  with `pii_pseudonymise` on input applies the session map, so the same address
  becomes the same `[EMAIL_n]` the log has; a manifest without it gets the
  one-way `redact_text` floor.
- **Agent identity, not model identity.** `/v1/models` advertises `spec.name`
  (`b2b-support`); any other `model` is a 404. A manifest can switch models
  without the UI being reconfigured, and the UI's id should mean "this agent".
- **One turn per conversation at a time.** A second request while a run is in
  flight is a 409, the same rule the page uses.
- **Streaming is live only when it is safe.** With no `on_output` guardrail,
  model deltas are forwarded as `chat.completion.chunk` frames while the run is
  in flight. With one, the answer is buffered until the guardrail has run and
  then emitted in chunks: a token stream is a second answer path, and it would
  show the user a commitment that `commitment_check` is about to deny (or text
  another output guardrail is about to rewrite). `[DONE]` always ends the stream;
  a failure is an SSE `error` frame, not a truncated connection.
- **Bounded requests.** One message is capped at 32k characters, a conversation
  at 200 messages / 200k characters, and a non-text content part is a 400. These
  are request-body guards, not run limits: the manifest's `limits` still govern
  the turn.
- **LibreChat is configuration.** The example
  (`examples/librechat/librechat.yaml`) sends
  `X-Conversation-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'`, sets
  `titleConvo: false` (a title request has no user message and would be a 400),
  and lists one model, the agent's name. The `librechat` compose profile runs the
  UI and its MongoDB; the harness stays on the host.

## Consequences

The harness can now be demoed in someone else's chat UI, and the honest parts of
it survive: the loop runs, guardrails fire, every turn is an event, and a
side-effecting tool cannot execute. `make web` remains the place where approvals
are *shown* — the two surfaces are complementary, not duplicates.

Costs, stated rather than implied:

- **The approval beat is missing.** A refund is impossible from LibreChat. That is
  correct behaviour and a worse demo; the ADR-0023 page is still the one that
  shows hard rule 4 as a decision rather than a refusal.
- **History is the client's.** The page rebuilds history from the log (ADR-0005,
  ADR-0023); this surface takes the `messages` array it is sent. Edit-and-resend
  works, and a client that rewrites history gets a conversation the log did not
  have. Both are in the log as separate runs, so nothing is lost, but the log is
  no longer the single source of a `/v1` conversation. The client's history also
  contains the user's **raw** text, so it is cleaned before it reaches the model:
  with `pii_pseudonymise` on input, the session map turns the address into the
  same `[EMAIL_n]` the log has (and the vault resolves it); without it, the
  one-way `redact_text` floor applies, and `[REDACTED]` cannot be resolved.
- **Sentinels still depend on the header.** Without `X-Conversation-Id` there is
  no safe session, which is why the request is refused instead of answered.
- **No identity.** The token is shared, so "who asked" is still unknown (ADR-0023
  says the same). `X-User-Id` is not used, because trusting a client-supplied
  identity in an audit trail would be worse than having none.
- **Two write paths in one process.** They share the log, the runner and the
  limiter; a bug in one is a bug in both, and both are covered by tests.
- **A guardrailed manifest streams late.** The demo has `commitment_check`, so
  the first token arrives after the run, not while the model writes. That is the
  price of not having a second, unguarded answer path; a manifest with no
  `on_output` guardrail streams live.
- **The exposure grows.** A containerised LibreChat needs a non-loopback bind, so
  the operator either terminates TLS at a proxy or sets
  `RIGGING_SERVICE_ALLOW_PLAINTEXT=1` for a network that already protects the
  link. The token still crosses the Docker bridge in clear text, and it remains a
  shared secret, not an identity (ADR-0021, ADR-0023).

## Alternatives rejected

- **Proxy the model instead of the agent** (point LibreChat at the gateway): demo
  of a model, no tools, no guardrails, no events — the thing ADR-0023 already
  refused.
- **Auto-approve side effects so the UI demo looks complete:** removes hard rule 4
  from the only surface most people will try. A demo that cannot show a denial is
  not showing the harness.
- **Surface tool calls as OpenAI `tool_calls`:** the client would have to answer
  with `tool` messages carrying results it cannot produce (the tools read the
  fixture DB), and approvals would still be missing. The event log is where a tool
  call is read.
- **A second web server:** two processes, two token checks, two rate limiters, and
  a second place to forget the approval policy. The existing app already owns the
  log, the runner and the limiter.
- **Session per request with an in-memory sentinel map:** reintroduces exactly the
  cross-conversation leak ADR-0027 sealed, with none of the persistence.
