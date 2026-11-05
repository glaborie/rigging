# Red team

promptfoo probes pointed at the b2b-support agent. This directory is the tooling;
[`../docs/red-team.md`](../docs/red-team.md) is what the scans found, what they
deliberately do not cover, and why the judgement is split between a deterministic
assertion and a model grader.

Nothing here is part of the `rigging` package. There is no ADR for it for the same
reason there is none for `scripts/bench.py`: no protocol, layer or hook changed.

## The files

| File | Is | Edit it? |
|---|---|---|
| `provider.py` | the target: one turn through `run_agent`, then the facts | yes, it is code |
| `controls.yaml` | four known-good cases — run before every scan | yes |
| `promptfooconfig.yaml` | unattended posture: nothing may run unapproved | yes |
| `promptfooconfig.approved.yaml` | `refund_order` pre-approved: the guardrails are on trial | yes |
| `redteam.yaml` | probes generated for the unattended scan | **no** — regenerated |
| `redteam.approved.yaml` | probes generated for the approved scan | **no** — regenerated |
| `run.sh` | one entry point, so the environment is right every time | yes |

The two generated files are committed on purpose: a reviewer can read the actual
attacks without spending a token, and `promptfoo eval -c` reruns them with no
generation cost. Editing them is pointless — `run.sh` overwrites them.

## Run it

```bash
redteam/run.sh controls        # 4 known-good cases, ~40s
redteam/run.sh scan            # 18 probes, ~3 min
redteam/run.sh scan-approved   # 15 probes, ~2 min
```

Needs `npx` (Node 18+), the project venv (`make install`), and `.env` with
`RIGGING_PROFILE_OPENROUTER_API_KEY` (the target) and `OPENROUTER_API_KEY`
(probe generation and grading).

`run.sh` does four things a bare `npx promptfoo` gets wrong:

- runs the Python worker from `.venv` (`PROMPTFOO_PYTHON`), or `provider.py`
  cannot import `rigging`;
- sources `.env`. promptfoo resolves `openrouter:` keys from the *process*
  environment; without this the generator and the grader send an empty key and
  OpenRouter answers `401 User not found` — a broken grader that reads as a
  finding;
- exports one Langfuse trace per probe when `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` are
  set, and refuses to start when `[langfuse]` is missing, because the loop
  swallows exporter errors by design (ADR-0012) and the scan would export nothing
  and report success. `REDTEAM_EXPORT= redteam/run.sh scan` turns traces off;
- splits `redteam generate` from `eval`, because `redteam run -o` writes the
  *results* envelope over the generated probes and leaves a file that cannot be
  re-evaluated.

Anything after the mode is passed to the eval:

```bash
redteam/run.sh scan --no-cache -o /tmp/results.json   # fresh eval + evidence file
redteam/run.sh scan                                   # generation is skipped if unchanged
```

## The provider's contract

promptfoo calls `call_api(prompt, options, context)` once per probe. `prompt` is
the attack; `options["config"]` and `context["vars"]` carry the knobs; the return
value is a JSON string of facts:

```json
{"answer": "I couldn't complete the refund — the approval was denied…",
 "status": "ok",
 "attempted": ["lookup_order", "refund_order"],
 "executed": ["lookup_order"],
 "denied": ["refund_order"],
 "approval_denials": ["refund_order"],
 "guardrail_denials": [],
 "side_effects": [],
 "writes": {"order_notes": [], "new_order_rows": 0, "new_notifications": 0},
 "emails": [], "stored_contacts_plaintext": 0, "pii_key_set": true,
 "trace": {"exporters": ["langfuse"], "name": "redteam policy#2", "tags": ["…"]}}
```

Two knobs, both overridable per test through `vars`:

```yaml
config:
  approve: false            # deny everything (the unattended default)
  approve: true             # a fully permissive operator
  approve: [refund_order]   # only this tool — the guardrails are what is tested
  export: langfuse          # overlay the manifest's exporters; '' means none
```

`attempted` comes from `tool_call` events, which are written *before* the
guardrails and the approval policy see the call; `executed` comes from
`tool_result` with `denied` and `is_error` both false. Counting attempts as
actions is what made `refund-before-delivery-blocked` flaky in the eval suite, so
the two are never merged. `writes` is a diff of the fixture database around the
turn — the ground truth the transcript cannot fake.

## Adding a probe

Generated plugins may be unreliable for your case; hand-written tests are welcome
in `controls.yaml` (they use the same provider and assertions). To add a
generated plugin, edit the `redteam.plugins` list in a config and rerun — no
`--force` needed, a config change is detected:

```yaml
plugins:
  - id: policy
    numTests: 3
    config:
      policy: >-
        One boundary, named, with the allowed exception. Vague policies produce
        vague probes.
```

Keep the custom `policy` plugins pointed at what the manifest actually enforces
(approval, the 30-day window, the 500 USD cap, contact confidentiality). A plugin
whose failure nobody claims the agent protects is noise, and the score stops
meaning anything.

## Reading a failure

In this order:

1. `side_effects` and `writes` — did the world change? That is a finding.
2. `executed` / `approval_denials` / `guardrail_denials` — which layer allowed or
   blocked it.
3. `answer` — what the customer would have been told.
4. The grader's `reason` last. `Could not extract JSON from llm-rubric response` is
   grader noise; a probe can also fail once and pass on a rerun with the same
   transcript. The deterministic assertion is the one that does not move.

Every probe is a real run in the harness's own log:

```bash
uv run harness runs --db runs/redteam/events.db
uv run harness runs --db runs/redteam/events.db --run r_...
```

With `REDTEAM_EXPORT=langfuse`, `"trace"` in the facts is the trace's name in
Langfuse — open it to see the `chat` and `execute_tool` spans, arguments and the
denied result.

## Gotchas

- **Paths in a config are relative to that config.** `file://provider.py` resolves
  next to `promptfooconfig.yaml`, so a copy of a config in `/tmp` needs an
  absolute `file:///…/redteam/provider.py`.
- **`maxConcurrency: 1`, both scans.** The provider re-seeds one fixture DB
  (`runs/redteam/demo.db`) per call; concurrent calls would race on it.
- **The fixture is a copy.** Probes never touch
  `examples/b2b-support/demo.db`. `ORD-1002`'s delivery date is moved to five days
  ago in that copy, so the in-window control does not rot when the seed's pinned
  date falls out of the 30-day window.
- **`runs/redteam/` is gitignored** (the event log and the fixture copy). The
  generated probes are not — they are the reviewable artifact.
