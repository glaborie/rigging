# LibreChat as a front end (ADR-0028)

LibreChat can drive one rigging agent through the OpenAI-compatible surface that
`harness serve --openai-api` mounts. It is a real harness run in that UI: tools,
guardrails, retrieval, the event log and the exporters are all still the loop's,
and `harness runs` shows the same turn LibreChat just rendered.

What it is **not**: an approval channel. LibreChat cannot ask a human whether
`refund_order` may run, so the bridge replaces the manifest's `cli` policy with
auto-deny. A side-effecting tool answers "denied by approval policy" in that UI.
Use `make web` (the page) or `make chat` when the demo is the approval beat.

## Quickstart

Three terminals, or two if you stop the harness when you are done:

```bash
# 1. one shared secret, in .env
#    RIGGING_SERVICE_TOKEN=<a long random string>
#    RIGGING_LIBRECHAT_BASE_URL=http://host.docker.internal:8000/v1

# 2. the harness, with the OpenAI surface
RIGGING_SERVICE_TOKEN=demo make openai
#    http://0.0.0.0:8000/v1/chat/completions
#    token: demo; agent: b2b-support; conversation header required

# 3. LibreChat
make librechat
#    http://localhost:3080 — register the first account (it becomes admin),
#    pick the "rigging" endpoint, send a message.
```

`make openai` sets `RIGGING_SERVICE_ALLOW_PLAINTEXT=1` because the harness binds
`0.0.0.0` so the container can reach it. That flag is only acceptable because the
Docker bridge is local; for anything else, terminate TLS at a proxy and bind
loopback (see [`threat-model.md`](threat-model.md) §5). The token still crosses
that bridge in clear text.

Without Docker:

```bash
RIGGING_SERVICE_TOKEN=demo uv run harness serve \
  --manifest examples/b2b-support/agent.yaml --openai-api \
  --host 127.0.0.1 --port 8000
```

and point a LibreChat instance at `http://127.0.0.1:8000/v1` instead. The rest of
the example config is [`examples/librechat/librechat.yaml`](../examples/librechat/librechat.yaml).

## The config that matters

```yaml
endpoints:
  custom:
    - name: 'rigging'
      apiKey: '${RIGGING_SERVICE_TOKEN}'
      baseURL: '${RIGGING_LIBRECHAT_BASE_URL}'
      models:
        default: ['b2b-support']
        fetch: true
      titleConvo: false
      headers:
        X-Conversation-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'
```

- **`X-Conversation-Id` is required.** The harness hashes it into the session id,
  which is what lets the sentinel vault (ADR-0027) resolve an `[EMAIL_1]` from the
  previous turn. A request without it is a 400 that says where to set it.
- **The model id is the agent's name** (`b2b-support`), not
  `deepseek/deepseek-v4.1-flash`. The manifest owns the model; `/v1/models` lists
  the agent so switching models in `agent.yaml` does not change LibreChat's
  config.
- **`titleConvo: false`.** Titles are extra model calls with no user prompt; the
  server would answer them with a 400 and log a run for each one.
- **`fetch: true`** reads `/v1/models` with the same bearer token. If the harness
  is down, LibreChat falls back to `models.default`.
- **One token, not a rotation list.** `RIGGING_SERVICE_TOKEN` may be a
  comma-separated list for the harness, but LibreChat sends one `apiKey` value;
  use a single token here (and update it when you rotate).

## What works, what does not

| Works | Why |
|---|---|
| multi-turn conversation | the request's `messages` are the history; the manifest supplies the system prompt |
| tools and retrieval | `run_agent` runs them; LibreChat sees only the final text |
| guardrails | input, tool-call, tool-result and output hooks all run in the loop |
| PII sentinels across turns | the conversation id keys the session's vault; the client's raw history uses that map (or `redact_text` when the manifest has no PII guardrail) |
| the event log | every turn is a real run: `harness runs`, `harness escalations`, the page |
| SSE streaming | live deltas, except when an `on_output` guardrail buffers the answer |
| Langfuse / MLflow | `EXPORT=langfuse make openai`, same as every other entry point |

| Does not work | Why |
|---|---|
| approving a refund in LibreChat | the OpenAI protocol has no approval channel; the loop denies |
| images or audio | text only; a non-text content part is a 400, not silently dropped |
| editing the manifest from the UI | one manifest per `harness serve`; the UI is a client |
| per-user identity | the bearer token is a shared secret, not a user |

The last row matters for a deployment: anyone with the token can start a run, and
the conversation id is a key the client supplies, not proof of who owns it. The
service's threat model treats that as a single-operator demo, not a multi-tenant
chat server.

## Verifying without LibreChat

```bash
RIGGING_SERVICE_TOKEN=demo uv run harness serve \
  --manifest examples/b2b-support/agent.yaml --openai-api --host 127.0.0.1 &

curl -s http://127.0.0.1:8000/v1/models \
  -H 'Authorization: Bearer demo'

curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer demo' \
  -H 'X-Conversation-Id: conv-1' \
  -H 'Content-Type: application/json' \
  -d '{"model":"b2b-support","messages":[{"role":"user","content":"What is the refund policy for ORD-1002?"}]}'
```

The response has OpenAI's `choices` + `usage` shape plus a `rigging` object with
the `run_id` and `session_id`; the same run is in `harness runs` and on the page.

Streaming is the same request with `"stream": true`; the frames are
`chat.completion.chunk` and end with `data: [DONE]`. With an `on_output` guardrail
(the demo has `commitment_check`) the first token arrives after the run finishes:
the stream is a delivery mechanism, not a shortcut around the policy.
