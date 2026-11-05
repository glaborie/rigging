#!/usr/bin/env bash
# promptfoo entry point for the b2b-support red team.
#
#   redteam/run.sh controls        # known-good controls (run before every scan)
#   redteam/run.sh scan            # unattended posture: nothing may run unapproved
#   redteam/run.sh scan-approved   # refunds pre-approved: the guardrails are on trial
#
# Each scan keeps its generated probes next to its config (`redteam/redteam*.yaml`),
# so the probes are reviewable and a rerun is `eval -c <that file>` with no
# generation cost. Anything after the mode is passed to the eval.
#
# Anything after the mode is passed through to promptfoo, e.g.
#   redteam/run.sh scan --max-concurrency 1
#
# Two things this pins that a bare `npx promptfoo` would get wrong:
#   * the Python worker must be the project venv, or the provider cannot import
#     `rigging`;
#   * one Langfuse trace per probe when LANGFUSE_* is configured (see the
#     REDTEAM_EXPORT block below), core traces from the same exporter as `make
#     trace`;
#   * results stay local: `redteam run` only uploads when you ask it to
#     (`promptfoo share`), and the controls eval passes `--no-share` explicitly.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v npx >/dev/null 2>&1 || {
  echo "redteam: npx not found — install Node 18+ to run promptfoo" >&2
  exit 2
}
[ -x .venv/bin/python ] || {
  echo "redteam: no .venv — run 'make install' first" >&2
  exit 2
}

export PROMPTFOO_PYTHON="$PWD/.venv/bin/python"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

# promptfoo resolves `openrouter:` / `openai:` keys from the *process* environment,
# and this repo keeps them in `.env` (which only pydantic-settings reads for the
# harness itself). Without this, the generator and the grader send an empty key
# and OpenRouter answers 401 "User not found" — a broken grader that reads as a
# finding. `set -a` so every assignment is exported to npx and to the worker.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Trajectories: one Langfuse trace per probe, when this machine can send them.
# `REDTEAM_EXPORT=` (empty) disables it; anything else is passed to the provider,
# which overlays the manifest's exporters (the shared manifest stays key-free).
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
  export REDTEAM_EXPORT="${REDTEAM_EXPORT-langfuse}"
else
  export REDTEAM_EXPORT="${REDTEAM_EXPORT:-}"
fi
if [ -n "$REDTEAM_EXPORT" ] && ! .venv/bin/python -c "import opentelemetry.sdk.trace" 2>/dev/null; then
  echo "redteam: REDTEAM_EXPORT=$REDTEAM_EXPORT needs the [langfuse] extra:" >&2
  echo "         uv sync --extra dev --extra langfuse   (or run with REDTEAM_EXPORT= )" >&2
  exit 2
fi
echo "redteam: traces -> ${REDTEAM_EXPORT:-none}" >&2

mode="${1:-controls}"
shift || true

case "$mode" in
  controls)
    exec npx --yes promptfoo@latest eval -c redteam/controls.yaml --no-cache --no-share "$@"
    ;;
  scan|scan-approved)
    # `redteam run` = generate + eval, but its `-o` writes the *results* envelope
    # over the generated probes, which makes the suite un-rerunnable. Two steps
    # instead: probes to a file that can be committed and diffed, then `eval`
    # against them. `-o results.json` on the eval is the evidence a reviewer wants.
    if [ "$mode" = "scan" ]; then
      config=redteam/promptfooconfig.yaml
      probes=redteam/redteam.yaml
    else
      config=redteam/promptfooconfig.approved.yaml
      probes=redteam/redteam.approved.yaml
    fi
    npx --yes promptfoo@latest redteam generate -c "$config" -o "$probes"
    exec npx --yes promptfoo@latest eval -c "$probes" "$@"
    ;;
  *)
    echo "usage: redteam/run.sh [controls|scan|scan-approved] [promptfoo args...]" >&2
    exit 2
    ;;
esac
