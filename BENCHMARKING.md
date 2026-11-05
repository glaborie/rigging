# Benchmarking

`scripts/bench.py` runs `harness eval` N times per model and writes a results
table. It is a thin driver: all the assertions live in the eval suite, and all
the provider routing lives in the harness. Getting a benchmark to produce
numbers instead of crashes is therefore mostly about two strings — the suite
path and the model id — plus the credential profile.

`make bench` fills in the demo's defaults:

```bash
make bench                                    # b2b suite, deepseek, 5 repeats, Langfuse traces
make bench REPEATS=1 RUN_NAME=smoke           # one pass, into results/smoke/
make bench MODELS="qwen3:8b" PROFILE=ollama   # a local model, no gateway container
make bench EXPORT="langfuse mlflow"           # one trace per case *and* one MLflow run per case
```

Underneath it is this command — use it directly when you need a flag the target
does not expose:

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

## `make bench`

`make bench` is one line in the Makefile over `scripts/bench.py`; the script
still owns the behaviour. What the target pins:

| Variable | Default | Meaning |
|---|---|---|
| `SUITE` | `examples/b2b-support/eval.yaml` | suite file |
| `MODELS` | `opencode-go/deepseek-v4.1-flash` | one or more model ids, space separated |
| `REPEATS` | `5` | passes per model |
| `EXPORT` | `langfuse` | exporter(s), space separated → repeated `--export`; `EXPORT=none` passes `--no-export` |
| `PROFILE` | `default` | credential profile (`default` = `$RIGGING_BASE_URL`, e.g. OmniRoute) |
| `RUN_NAME` | UTC start time | name of this run's directory under `results/` |
| `BENCH_OUT` | `results` | parent directory for run directories |
| `TIMEOUT` | script default (900) | per-run seconds |
| `VERBOSE` | off | `VERBOSE=1` streams the eval's whole output (`-v`) |
| `MODEL` | (unset) | not used here — `make bench` takes the plural `MODELS` |

Two consequences of the defaults: the target always overrides the manifest's
pinned `credential_profile` with `PROFILE`, and it always exports unless you ask
otherwise — which is why `EXPORT=none` exists (no keys, no network, slightly
faster). The remaining flags (`--safety-cases`, `--json`, a custom `--cmd`) stay
on the script.

Every invocation writes its own `results/<run-name>/` directory, so a repeat
never overwrites an earlier run's evidence. See [Reading the output](#reading-the-output).

## The three things that go wrong

### 1. The suite path is `eval.yaml`, not `suite.yaml`

`examples/b2b-support/` contains `agent.yaml` (the manifest) and `eval.yaml`
(the eval cases). There is no `suite.yaml`; the CLI's help text says "path to
suite.yaml" only because that is the generic name for the file.

A wrong path does not fail loudly. `bench.py` treats a run that produced no
parsable cases as a **crashed run**, so you get a table full of zeroes and the
reason is buried in the run's `raw/<model>-0.txt`:

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
| `--out` | `results` | parent directory; each invocation gets its own subdirectory |
| `--run-name` | UTC start time | name of this run's directory (`results/<run-name>/`) |
| `-v`, `--verbose` | off | echo the eval's full stdout/stderr as it arrives |
| `-q`, `--quiet` | off | no per-case progress lines; only rep summaries and the table |
| `--export` | `langfuse` | exporter for every eval run, repeatable |
| `--no-export` | off | no traces at all: nothing to configure, slightly faster |

## Watching a run

The driver streams: each eval case prints one line on stderr the moment it
finishes, so an 18-case suite that takes eight minutes never looks hung.

```
suite examples/b2b-support/eval.yaml · 1 model(s) · 5 repeats · 5 runs · timeout 900s
results: results/2026-11-05T08-16-33Z

[1/5] opencode-go/deepseek-v4.1-flash rep 1/5 · 08:16:33
    [1/14] refund-in-window-approved   PASS   6.2s
    [2/14] over-authority-guardrail    PASS   9.1s
    ...
[1/5] opencode-go/deepseek-v4.1-flash rep 1/5: 18/18 cases pass, rc=0, 2m20s · ~9m20s left
```

The `[i/n] case PASS|FAIL` lines come from `harness eval` itself (one per case,
on stderr, flushed), which is why a plain `uv run harness eval ...` is no longer
silent either — use `--no-progress` there to turn them off. When `--cmd` runs
something other than `harness eval`, those lines are simply absent and the
driver falls back to one summary line per rep.

`-v` forwards the child's entire stdout and stderr, prefixed with `  │`, for the
case where a run fails in a way the summary line does not explain. It does not
change what lands in `raw/`.

## Traces in Langfuse

Every rep runs with `--export langfuse`, and `bench.py` tags the trace with
`bench,<model>,rep-<n>`. One eval case is one trace, so a 5-repeat run of the
18-case b2b suite is 90 traces — filter by tag in Langfuse, not by name.

```
tags: ["bench", "opencode-go/deepseek-v4.1-flash", "rep-1"]
```

Tags come from `RIGGING_TRACE_TAGS`, which `bench.py` sets on each subprocess;
`loop._trace_tags()` turns it into `langfuse.trace.tags` on the root span
(ADR-0012 postscript) and into the `rigging.trace_tags` attribute that the
MLflow exporter writes as its `rigging.tags` run tag. They are labels, not
content, so they survive `capture_content: false`.

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

### Traces in MLflow

MLflow is a run-level backend (ADR-0020): one eval **case** becomes one MLflow
run with the metrics (tokens, steps, latency, cost, chat/tool call counts), so a
5-repeat run of an 18-case suite is 90 runs in one experiment, not one run with
70 children. What makes them usable:

- all 70 are tagged `rigging.tags = bench,<model>,rep-<n>` — filter on that tag
  or search the run name; without it every run is called `rigging` and five
  repeats are indistinguishable;
- the model is a param/tag (`gen_ai.request.model`), the status a tag
  (`rigging.status`, `FINISHED` only when the run ended `ok`).

MLflow needs a tracking server that already has the experiment you point it at.
`MLFLOW_EXPERIMENT_ID` is an **id, not a name**, and the exporter never creates
the target. Three things can therefore be wrong, and all three fail at assembly —
before the first model call — with exit code 2:

```
harness run: mlflow experiment '1' (rigging-bench) on http://localhost:5000 is deleted.
MLflow never reuses an id, so a deleted and re-created experiment has a new one:
`uvx mlflow experiments search` lists the ids, then update MLFLOW_EXPERIMENT_ID
```

```
harness run: no mlflow experiment with id '99' on http://localhost:5000. Create it first
(`uvx mlflow experiments create --experiment-name <name>`) and set MLFLOW_EXPERIMENT_ID
to the id it prints
```

```
harness run: mlflow tracking server unreachable at http://localhost:5000: ...
```

Why it fails rather than warns: the *running* loop deliberately swallows exporter
errors so observability can never fail a run (ADR-0012, ADR-0020 postscript), which
means a wrong target records nothing and still reports `ok`. Validating at assembly
is the other half of that bargain. `bench.py` runs the same check up front and
refuses to start, rather than spending 85 model calls on runs that go nowhere:

```
error: --export mlflow cannot record runs, so the benchmark was not started:
  no mlflow experiment with id '99' on http://localhost:5000. ...
```

The deleted-experiment case is the one that actually bites: `uvx mlflow
experiments delete --experiment-id 1` followed by a re-create gives you id `2`, not
`1`. `uvx mlflow experiments search` is the source of truth (add `--view-type ALL`
to see deleted ones).

Start a server for a local benchmark; no `[mlflow]` extra exists because the
exporter speaks the REST API over `httpx` and never imports the SDK:

```bash
uvx mlflow server --backend-store-uri sqlite:///mlflow.db --port 5000
uvx mlflow experiments create --experiment-name rigging-bench   # -> "Created ... with id 1"
MLFLOW_EXPERIMENT_ID=1 uv run python scripts/bench.py ...        # experiment 0 is "Default"
```

An `MLFLOW_TRACKING_TOKEN` is sent as a bearer header on every tracking call
(including the validation) for servers that require auth.


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

Each invocation writes its own directory — `results/<run-name>/`, where the run
name is the UTC start time unless `--run-name` says otherwise. A rerun therefore
never overwrites the previous run; a reused `--run-name` gets `-2`, `-3` appended
rather than sharing a folder. Three things live there:

- `raw/<model>-<rep>.txt` — the run's stdout, plus a `--- stderr ---` section
  when the run wrote anything there (the per-case progress lines included). This
  is the only place a crash explains itself. The model id is sanitised into the
  filename: `opencode-go/deepseek-v4.1-flash` becomes
  `opencode-go_deepseek-v4.1-flash`, `openrouter:foo/bar` becomes
  `openrouter_foo_bar`.
- `summary.json` — per-model rates, medians and crashed-run counts.
- `table.md` — the published table.

Runs recorded before this layout sit flat in `results/` (`results/raw/`,
`results/table.md`); they are evidence and were not moved.

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

## Practising without touching the published table

Nothing to do: every invocation is already isolated in its own directory, so a
one-repeat smoke run cannot clobber the published one.

```bash
make bench REPEATS=1 RUN_NAME=smoke
# -> results/smoke/{raw/,summary.json,table.md}

uv run python scripts/bench.py examples/b2b-support/eval.yaml \
  --models "opencode-go/deepseek-v4.1-flash" --repeats 1 \
  --cmd "uv run harness eval {suite} --model {model} --profile default" \
  --run-name smoke
```

`BENCH_OUT=/tmp/bench-check` (or `--out`) still works when you want the run off
the repository's disk entirely.

`results/` is gitignored, so a run never shows up in `git status` and nothing
has to be cleaned up afterwards. What is kept is the run you point at in a
review: the numbers below, and the `table.md` of the run that produced them.

A kept run should be one that completed; failed runs are worth keeping when
they document a real failure mode (the `opencode-go/glm-5.2` crash and the
retired-model 404s are kept for that reason), not when they are a typo in the
command.

## Slow and flaky providers

A hosted model through OmniRoute takes anywhere from a few seconds to a minute
per case depending on how many tool steps it needs, so 5 repeats of a 14-case
suite is several minutes. `--timeout` must exceed the slowest single run,
otherwise a slow case is recorded as a crash:

```bash
uv run python scripts/bench.py ... --timeout 1800
```

## Current results

The published table is the `table.md` of whichever run was promoted into
`results/table.md`. As of v0.6.3:

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
