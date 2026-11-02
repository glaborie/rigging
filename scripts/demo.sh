#!/usr/bin/env bash
# The 60-second demo. Recorded by scripts/record_demo.py into docs/demo.cast,
# rendered to docs/demo.gif by scripts/cast_to_gif.py (`make record` does both).
#
# Safe to run by hand: it uses host Ollama (no gateway container) and a scratch
# event log, so it never touches your rigging.db.
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${MODEL:-qwen3:8b}"
DB="${DB:-/tmp/rigging-demo.db}"
rm -f "$DB"

step() { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; }

step "uv run harness tools"
uv run harness tools --kind tool

step "uv run python examples/b2b-support/seed.py"
uv run python examples/b2b-support/seed.py

step "uv run harness run examples/hello/agent.yaml 'Say hello in one sentence.'"
uv run harness run examples/hello/agent.yaml "Say hello in one sentence." \
  --model "$MODEL" --db "$DB"

step "uv run harness run examples/b2b-support/agent.yaml 'Refund ORD-1002 for \$112.50.'"
uv run harness run examples/b2b-support/agent.yaml "Refund ORD-1002 for \$112.50." \
  --model "$MODEL" --db "$DB" --yes

RUN=$(uv run harness runs --db "$DB" --limit 1 | awk 'NR==2 {print $1}')
step "uv run harness runs --run $RUN     # every step is an event"
uv run harness runs --db "$DB" --run "$RUN" | sed -n '1,7p' | cut -c1-108

printf '\n\033[1;36mthe same runs, live: make serve -> http://127.0.0.1:8000\033[0m\n'
