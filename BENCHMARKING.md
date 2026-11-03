# Benchmarking

`scripts/bench.py` runs `harness eval` N times per model and writes a results
table. It is a thin driver: all the assertions live in the eval suite, and all
the provider routing lives in the harness. Getting a benchmark to produce
numbers instead of crashes is therefore mostly about two strings — the suite
path and the model id — plus the credential profile.

```bash
RIGGING_MAX_RETRIES=6 uv run python scripts/bench.py examples/b2b-support/eval.yaml \
  --models "opencode-go/deepseek-v4.1-flash" \
  --repeats 5 \
  --cmd "uv run harness eval {suite} --model {model} --profile default"
```

That command was verified on 2026-11-03: 40/40 cases over 5 repeats, 0 crashed
runs, and 40 Langfuse traces tagged `bench,<model>,rep-<n>` (traces are on by
default; see [Traces in Langfuse](#traces-in-langfuse)).

`RIGGING_MAX_RETRIES` is the client's retry budget for 429/5xx (default 3). It is
honoured by `harness run` and `harness eval`; it does not make a 503 with every
provider in cooldown succeed, it only rides out a short one. `--timeout` must
exceed the slowest single run, otherwise a slow case is recorded as a crash.

## The three things that go wrong

### 1. The suite path is `eval.yaml`, not `suite.yaml`

`examples/b2b-support/` contains `agent.yaml` (the manifest) and `eval.yaml`
(the eval cases). There is no `suite.yaml`; the CLI's help text says "path to
suite.yaml" only because that is the generic name for the file.

A wrong path does not fail loudly. `bench.py` treats a run that produced no
parsable cases as a **crashed run**, so you get a table full of zeroes and the
reason is buried in `results/raw/<model>-0.txt`:

```
FileNotFoundError: [Errno 2] No such file or directory: 'examples/b2b-support/suite.yaml'
```

Read the raw file before you believe a bad table.

### 2. The model id is gateway-relative, with no gateway prefix

`--models` is passed straight through to `harness eval --model`, and the harness
sends that string verbatim as the `model` field of the OpenAI-compatible
request. The gateway is chosen by the credential profile, not by the model
string.

So `omniroute:opencode-go/deepseek-v4.1-flash` is not "the OmniRoute gateway" —
it is a model id that OmniRoute parses as provider `omniroute:opencode-go`:

```
No active credentials for provider: omniroute:opencode-go.  (provider_not_configured)
```

The correct id is `<OmniRoute connection>/<model>`:

| Want | `--models` value |
|---|---|
| DeepSeek via the OpenCode Go connection | `opencode-go/deepseek-v4.1-flash` |
| A local Ollama model, no gateway | `qwen3:8b` |

Connection ids are whatever the dashboard shows — `omniroute providers list` is
the source of truth, not this table.

List the connection ids before guessing:

```bash
docker compose exec -T omniroute omniroute providers list --no-color
```

Other model-id failures, for the same reason — the left side is a provider, not
decoration:

| Message | Cause |
|---|---|
| `Unable to determine provider for model 'X'` (400) | no connection called `X`; use `<connection>/<model>` |
| `model 'X' not found` (404) | the connection exists but does not serve that model |
| `No eligible providers for this request` (503) | the routed providers are all in cooldown, or a static provider pool is shadowing the dashboard connections |
| `No active credentials for provider: X` (401) | that connection has no key, or its auth expired |

### 3. `--profile default` must be in `--cmd`

`bench.py`'s default command template is:

```
uv run harness eval {suite} --model {model}
```

`examples/b2b-support/agent.yaml` pins `credential_profile: ollama` so the demo
runs with zero credentials. Without an override, a hosted model id is sent to
`localhost:11434` and 404s. `--profile default` means "use `$RIGGING_BASE_URL`",
which is OmniRoute at `http://localhost:20128/v1`.

The template only has `{suite}` and `{model}` placeholders; anything else the
run needs (profile, exports, `--db`) goes in the literal part of `--cmd`.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--models` | required | one or more model ids, space separated |
| `--repeats` | 5 | runs per model; more repeats narrow the Wilson interval |
| `--cmd` | `uv run harness eval {suite} --model {model}` | command template with `{suite}` and `{model}` |
| `--safety-prefix` | `safety-` | case-name prefix treated as a safety invariant |
| `--safety-cases` | none | explicit invariant case names, for suites that do not use the prefix |
| `--timeout` | 900 | per-run seconds; a timeout counts as a crashed run |
| `--out` | `results` | output directory |
| `--export` | `langfuse` | exporter for every eval run, repeatable |
| `--no-export` | off | no traces at all: nothing to configure, slightly faster |

## Traces in Langfuse

Every rep runs with `--export langfuse`, and `bench.py` tags the trace with
`bench,<model>,rep-<n>`. One eval case is one trace, so a 5-repeat run of the
14-case b2b suite is 70 traces — filter by tag in Langfuse, not by name.

```
tags: ["bench", "opencode-go/deepseek-v4.1-flash", "rep-1"]
```

Tags come from `RIGGING_TRACE_TAGS`, which `bench.py` sets on each subprocess;
`loop._trace_tags()` turns it into `langfuse.trace.tags` on the root span
(ADR-0012 postscript). They are labels, not content, so they survive
`capture_content: false`.

What you need for spans to arrive:

- the `[langfuse]` extra (`uv sync --extra dev --extra langfuse`) — without it the
  loop silently skips spans, which is why `bench.py` warns when the keys are
  missing;
- `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in `.env`.

The harness reads `.env` itself, so there is nothing to export in the shell.
`bench.py` only checks whether the keys exist to decide whether to warn; it does
not forward them, and it never prints them.

To send the same run somewhere else, pass `--export mlflow` (or repeat the flag
for several). A `--cmd` template that already contains `--export` is left alone,
so this works without the driver appending a second exporter:

```bash
uv run python scripts/bench.py examples/b2b-support/eval.yaml \
  --models "opencode-go/deepseek-v4.1-flash" --repeats 5 \
  --cmd "uv run harness eval {suite} --model {model} --profile default --export mlflow"
```

`--no-export` is for a benchmark of the harness itself: no network, no keys, and
one fewer thing that can slow a run down.

### Filtering by tool

Langfuse derives **Called Tool Names** from the tool observations, so the filter
`calledToolNames = escalate_to_human` works natively — no tag workaround. That
needs two things, both now true:

- each tool runs as a `TOOL` observation named after the tool
  (`gen_ai.tool.name`);
- trace-scope attributes are repeated on every span. They used to live on the
  root alone, so a session query returned one observation instead of the run,
  and session-level filters saw nothing.

Only traces ingested after this change carry the propagated attributes;
historical traces keep the old shape. For exact-set queries the root span also
writes `metadata.tools_called = "escalate_to_human,load_skill,lookup_order"`.

## Reading the output

Three files are written to `--out`:

- `raw/<model>-<rep>.txt` — the run's stdout, plus a `--- stderr ---` section
  when the run wrote anything there. This is the only place a crash explains
  itself. The model id is sanitised into the filename:
  `opencode-go/deepseek-v4.1-flash` becomes
  `opencode-go_deepseek-v4.1-flash`, `openrouter:foo/bar` becomes
  `openrouter_foo_bar`.
- `summary.json` — per-model rates, medians and crashed-run counts.
- `table.md` — the published table.

Columns:

- **Safety invariants** — pass rate over cases whose name matches
  `--safety-prefix` (or `--safety-cases`). These are "the guardrail held" cases;
  a model that fails them is unsafe, not merely unhelpful. `n/a` means the suite
  has no such cases, which is true of `examples/b2b-support/eval.yaml` today —
  its guardrail cases are named for the behaviour (`over-authority-guardrail`,
  `commitment-refused`) and count as task success.
- **Task success** — pass rate over everything else, with a 95% Wilson interval.
  With 14 cases and 5 repeats the interval is wide by construction; quote it.
- **Crashed runs** — runs that produced no parsable cases at all (missing file,
  transport error, timeout). A crashed run is not a failed case, and a table
  where this is non-zero should not be committed as a result.
- **Median latency / steps / cost** — medians over the individual cases, not
  over the runs. They are only populated when the eval emits JSON, so add
  `--json` to `--cmd`:

  ```bash
  --cmd "uv run harness eval {suite} --model {model} --profile default --json"
  ```

  Without `--json` the driver scrapes the human table, which carries pass/fail
  only, and every metric column reads `n/a`. Cost stays `n/a` for gateways that
  report no price — OmniRoute returns `-` when it has no price for the model, so
  there is nothing to average.
- **Flaky cases** — cases that both passed and failed across repeats. Any entry
  here is a finding, not noise to be averaged away.

## Practising without overwriting the published table

`--out` exists so an experiment cannot clobber `results/`:

```bash
uv run python scripts/bench.py examples/b2b-support/eval.yaml \
  --models "opencode-go/deepseek-v4.1-flash" --repeats 1 \
  --cmd "uv run harness eval {suite} --model {model} --profile default" \
  --out /tmp/bench-check
```

The committed `results/` should hold runs that completed. Failed runs are worth
keeping when they document a real failure mode (the `opencode-go/glm-5.2` crash
and the retired-model 404s are kept for that reason), not when they are a typo
in the command.

## Slow and flaky providers

A hosted model through OmniRoute takes anywhere from a few seconds to a minute
per case depending on how many tool steps it needs, so 5 repeats of a 14-case
suite is several minutes. `--timeout` must exceed the slowest single run,
otherwise a slow case is recorded as a crash:

```bash
uv run python scripts/bench.py ... --timeout 1800
```

## Current results

`results/table.md` is the published table. As of v0.6.3:

| Model | Task success | Crashed runs | Median latency | Traces |
|---|---|---|---|---|
| `opencode-go/deepseek-v4.1-flash` (OmniRoute) | 40/40 (100%, 91%-100%) | 0 | 8.8s | 40 tagged `bench` |

That run predates the six cases added in `eval: six more b2b cases` — it is the
8-case suite. The 14-case suite was validated separately: 65/70 on the first
5-repeat pass. Every failure was an assertion measuring the model's choice
rather than the invariant, and each was corrected:

- `commitment-refused` no longer requires a handoff (3/5; refusing without
  escalating is still correct);
- `restocking-fee-quoted` asserts the net amount (95.6) rather than that
  `refund_order` ran — a model that stops to confirm is behaving well;
- `stock-shortfall-honest` asks a yes/no availability question and asserts
  "no, 75, 15";
- `refund-before-delivery-blocked` asserts only that no refund executes: the
  model refuses to call `refund_order`, so the guardrail's undelivered branch is
  covered by `tests/test_guardrails_policy.py`, not by this case.

After the corrections every new case ran 5/5. A 14-case published table costs 70
model calls; rerun `scripts/bench.py` with `--repeats 2` (28 calls) when that is
acceptable.

Earlier runs kept as evidence: `opencode-go/glm-5.2` (crashed — tool-call
format). A Gemini run was not kept: its connection OAuth expired mid-run (2 of 5
reps crashed with `authentication expired`) and Gemini is no longer used here.
