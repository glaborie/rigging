# Showcase v0.7 — drive the agent from a browser

v0.6's web view could only read. v0.7 adds the one thing a demo needs: you type,
the harness runs, and the page shows what the loop did — including the approval
gate that stands between the model and a refund (ADR-0023).

Prerequisites:

```bash
make install                       # dev + service extras
make seed                          # deterministic demo.db (seed=7)
make check                         # 204 tests, ruff, mypy --strict
```

The transcript below is from `opencode-go/deepseek-v4.1-flash` through OmniRoute
(`PROFILE=default`). The standing rule still says the demo must pass on
`qwen3:8b`; this page only needs a model that calls tools.

## 1. Read-only is still the default

```bash
make serve                         # http://127.0.0.1:8000
```

The header reads **read-only view of the event log**, and there is no composer.
That is the whole of ADR-0018: the app has six GET routes and no POST. Prove it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -d '{"prompt":"hi"}' \
  http://127.0.0.1:8000/api/chat      # 404 — the route does not exist
```

## 2. Chat, tools and an approval

```bash
RIGGING_SERVICE_TOKEN=demo \
  make web MODEL=opencode-go/deepseek-v4.1-flash PROFILE=default
```

Open <http://127.0.0.1:8000>, enter the token when asked (it is kept in
`sessionStorage`, never in the URL), and send:

> Please refund ORD-1002 for 10 units, 112.50 USD. The customer says it arrived damaged.

What appears, in order, as the events arrive over SSE:

1. your message, then the agent's reply;
2. `lookup_order` — a card with the arguments and the JSON result;
3. `search_policies` twice — the refund window and the restocking policy;
4. a compact guardrail line per hook: `consequential_action Allow · refund_window Allow`,
   then `prompt_injection Allow` on the tool result;
5. `refund_order` — **side-effecting · needs approval**, with the arguments and
   `approve` / `deny`;
6. click `approve` → `approved · refund_order`, the tool result, and the answer.

The answer:

> Refund approved and recorded.
>
> - **ORD-1002** — $112.50 credit (10 of 120 × CBL-CAT6-10M @ $11.25/unit)
> - No restocking fee: goods returned sealed/unopened in original packaging (REF-002-restocking#1)
> - Remaining 110 units stay delivered
> - Appears as a credit note on the next invoice

The footer shows the run's model turns and token counts; `raw events` switches to
the unfiltered log, which is what the page was before v0.7.

What it proves: the page is a **client of the loop**. `POST /api/chat` calls the
same `run_agent` the CLI calls, the run id exists before the first event so the
page can subscribe, and the approval blocks the loop thread until the page
answers. The loop wrote `approval_requested` and `approval_verdict`; the page only
carried the answer.

## 3. Deny the refund

Same prompt, click `deny` instead. The tool result is a structured denial —
`denied by approval policy: denied in the web view` — and the run still finishes:
the model reads the denial and explains what it could not do. A demo that cannot
show a refusal is not showing the harness.

## 4. The evidence outlives the page

```bash
uv run harness runs                              # the run list the page reads
uv run harness runs --run <run_id>               # every event, in seq order
```

Every turn is a normal run: same event log, same redaction, same guardrails. Set
`observability.exporters: [langfuse]` in the manifest and the browser run emits a
Langfuse trace like any other — `make atif SESSION=<session_id>` turns it into an
ATIF trajectory for the Harbor viewer.

## The rules that make it safe

- **Opt-in.** Without `--manifest` there is no write route.
- **Token, even on loopback.** Any page the browser visits can POST to
  `127.0.0.1:8000`; the token is the one thing it cannot read. `harness serve`
  refuses `--manifest` without `RIGGING_SERVICE_TOKEN`.
- **One turn at a time.** A second POST while the session's run is `running` is a
  `409`, so two histories cannot interleave.
- **The manifest keeps its policy.** The page replaces `policy: cli` only;
  `auto_deny` still denies, with no buttons offered.
- **Ten minutes, then denial.** A tab that never answers does not pin a run
  forever.

`docs/threat-model.md` §3 (T8, T14) has the threat table for the write path.
