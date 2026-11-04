# rigging — everyday commands. `make` on its own prints this list.
#
# Choices you can override on the command line:
#   MANIFEST=examples/hello/agent.yaml     which agent to run (make web defaults to b2b-support)
#   PROMPT="..."                           the user message
#   MODEL=qwen3:8b                         override the manifest's model
#   EXPORT=langfuse                        also emit traces (repeatable; e.g. EXPORT=langfuse)
#   APPROVE=0|1                            allow side-effecting tools without asking (demo only)
#   DB=other.db                            alternate SQLite event log
#
# Gateway options (pick one, both are OpenAI-compatible HTTP):
#   OpenRouter          the demo default — set RIGGING_PROFILE_OPENROUTER_API_KEY in .env
#   make gateway        OmniRoute in Docker on :20128 — 375 providers, one endpoint
#   ollama pull qwen3:8b   host Ollama on :11434, no Docker, fully local (PROFILE=ollama)
#   RIGGING_BASE_URL=<any /v1 URL>   vLLM, LM Studio, OpenRouter, a friend's proxy

.DEFAULT_GOAL := help
SHELL := /bin/bash

MANIFEST ?= examples/hello/agent.yaml
WEB_MANIFEST ?= examples/b2b-support/agent.yaml
PROMPT   ?= say hello in one sentence
MODEL    ?=
EXPORT   ?=
HELLO_MODEL ?= freellm/codestral   # proven live through OmniRoute -> freellmapi
DB       ?=
APPROVE  ?= 0

.DELETE_ON_ERROR:
.PHONY: help install check test lint typecheck format gateway observability \
        retrieval hello demo run chat index eval up down logs clean trace serve record \
        test-browser browser-install

help: ## show this help
	@printf '\nrigging — modular agent harness\n\n'
	@printf 'Usage: make <target> [MANIFEST=... PROMPT="..." MODEL=...]\n\n'
	@printf 'Targets:\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nSetup:\n'
	@printf '  uv sync --extra dev      core + test/lint tooling (make install adds service, mcp)\n'
	@printf '  uv sync --extra qdrant   retrieval (v0.3)   [also: laya, langfuse, mcp, service, browser]\n'
	@printf '\nModel access (one is enough):\n'
	@printf '  RIGGING_PROFILE_OPENROUTER_API_KEY=...   in .env — the demo default (deepseek)\n'
	@printf '  ollama pull qwen3:8b     fully local, the weakest model the demo must pass on\n'
	@printf '  PROFILE=ollama           route a local model to host Ollama on :11434\n'
	@printf '\nCompose profiles:\n'
	@printf '  (default)      omniroute only — smallest footprint, enough to run an agent\n'
	@printf '  retrieval      + qdrant on :6345 (6333/6335/6340 are taken by other stacks)\n'
	@printf '\nTraces go to Langfuse Cloud: set LANGFUSE_HOST / _PUBLIC_KEY / _SECRET_KEY in .env.\n'
	@printf '  make observability       check those keys (runs no agent, emits nothing)\n'
	@printf '  make trace               run the example, then print the spans it produced\n'
	@printf '\nFirst run: make install && make gateway && make hello\n\n'

install: ## venv with dev + the extras the test suite exercises (service, mcp)
	uv sync --extra dev --extra service --extra mcp

check: ## what CI runs: lint + typecheck + tests (no network)
	$(MAKE) --no-print-directory lint typecheck test

test: ## unit + SSE wire fixtures + loop invariants (never touches the network)
	uv run pytest -m "not live and not browser"

test-live: ## opt-in: hit a real gateway (needs `ollama signin` or a pulled model)
	uv run pytest -m live

browser-install: ## install the Chromium that `make test-browser` drives
	uv sync --extra dev --extra service --extra mcp --extra browser
	uv run playwright install chromium

test-browser: ## opt-in: render the web view and run one turn in headless Chromium
	uv run pytest -m browser

lint: ## ruff
	uv run ruff check .

format: ## ruff --fix
	uv run ruff check --fix .

typecheck: ## mypy --strict on the public surface
	uv run mypy

gateway-password: ## reset the OmniRoute dashboard login: make gateway-password PW=new-password
	@scripts/omniroute-reset-password.sh "$(PW)" 2>/dev/null || \
	 (chmod +x scripts/omniroute-reset-password.sh && scripts/omniroute-reset-password.sh "$(PW)")

gateway: ## start OmniRoute (OpenAI-compatible, :20128)
	docker compose up -d omniroute
	@printf 'gateway: http://localhost:20128/v1  (model list: curl -s localhost:20128/v1/models)\n'

retrieval: ## gateway + Qdrant on :6345 (v0.3 index target)
	docker compose --profile retrieval up -d

observability: ## check the Langfuse Cloud credentials in .env (emits nothing)
	@test -f .env || { printf 'no .env — copy .env.example and fill LANGFUSE_* first\n'; exit 1; }
	@set -a; . ./.env; set +a; \
	 if [ -z "$$LANGFUSE_PUBLIC_KEY" ] || [ -z "$$LANGFUSE_SECRET_KEY" ]; then \
	   printf 'LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are empty in .env\n'; exit 1; fi; \
	 uv run python -c 'import opentelemetry.sdk, opentelemetry.exporter.otlp.proto.http.trace_exporter' 2>/dev/null || { \
	   printf 'missing the [langfuse] extra: uv sync --extra langfuse\n'; exit 1; }; \
	 printf 'langfuse cloud: %s\n' "$${LANGFUSE_HOST:-https://cloud.langfuse.com}"; \
	 printf 'credentials and OTel exporter look set. this target runs no agent, so it emits no spans.\n'; \
	 printf 'next: make trace  (runs the observability example, then reads the spans back)\n'

trace: ## run an agent with Langfuse spans, then read them back: make trace [MANIFEST=... PROMPT="..."]
	@marker="$${TMPDIR:-/tmp}/rigging-trace-marker"; rm -f "$$marker"; touch "$$marker"; \
	 uv run harness run $(if $(filter file,$(origin MANIFEST)),$(MANIFEST),examples/observability/agent.yaml) "$(PROMPT)" \
	   $(if $(MODEL),--model $(MODEL),) --export langfuse || exit $$?; \
	 uv run python scripts/langfuse_tail.py --since-file "$$marker" --wait 90

atif: ## export a Langfuse session as an ATIF trajectory: make atif SESSION=s_abc123 [HARBOR=runs/harbor]
	@test -n "$(SESSION)" || { printf 'usage: make atif SESSION=s_abc123 [OUT=runs/atif] [HARBOR=runs/harbor]\n'; exit 2; }
	uv run python scripts/langfuse_to_atif.py "$(SESSION)" --out $(if $(OUT),$(OUT),runs/atif) \
	  $(if $(HARBOR),--harbor $(HARBOR),)

hello: ## v0.1 smoke test: one prompt, no tools (proven end-to-end; see examples/hello/)
	RIGGING_BASE_URL=$${RIGGING_BASE_URL:-http://localhost:20128/v1} \
	uv run harness run $(MANIFEST) "$(PROMPT)" --model $(if $(MODEL),$(MODEL),$(HELLO_MODEL))

run: ## run any agent: make run MANIFEST=examples/b2b-support/agent.yaml PROMPT="..."
	uv run harness run $(MANIFEST) "$(PROMPT)" $(if $(MODEL),--model $(MODEL),) \
	  $(if $(PROFILE),--profile $(PROFILE),) \
	  $(if $(EXPORT),--export $(EXPORT),) \
	  $(if $(DB),--db $(DB),) $(if $(filter 1,$(APPROVE)),--yes,)

record: ## re-record docs/demo.cast + docs/demo.gif (needs host Ollama + ffmpeg)
	uv run python scripts/record_demo.py
	uv run python scripts/cast_to_gif.py

serve: ## read-only web view of the event log: http://127.0.0.1:8000
	uv run harness serve $(if $(DB),--db $(DB),) $(if $(PORT),--port $(PORT),)

web: ## chat in the browser, b2b agent by default (needs RIGGING_SERVICE_TOKEN)
	uv run harness serve $(if $(DB),--db $(DB),) $(if $(PORT),--port $(PORT),) \
	  --manifest $(if $(filter file,$(origin MANIFEST)),$(WEB_MANIFEST),$(MANIFEST)) \
	  $(if $(MODEL),--model $(MODEL),) $(if $(PROFILE),--profile $(PROFILE),) \
	  $(if $(EXPORT),--export $(EXPORT),)

chat: ## interactive REPL: make chat MANIFEST=examples/hello/agent.yaml
	uv run harness chat $(MANIFEST) $(if $(DB),--db $(DB),) $(if $(SESSION),--session $(SESSION),) \
	  $(if $(MODEL),--model $(MODEL),) $(if $(PROFILE),--profile $(PROFILE),) \
	  $(if $(EXPORT),--export $(EXPORT),)

seed: ## build the demo fixture DB: products, orders, policy chunks (deterministic)
	uv run python examples/b2b-support/seed.py

index: ## (re)build the retrieval index: no-op for fts5 (seed does it), embeds for qdrant
	uv run harness index $(MANIFEST)

demo: ## run the canonical b2b-support agent (needs the seeded demo.db: make seed)
	uv run harness run examples/b2b-support/agent.yaml "$(PROMPT)" \
	  $(if $(MODEL),--model $(MODEL),) $(if $(PROFILE),--profile $(PROFILE),) \
	  $(if $(EXPORT),--export $(EXPORT),) $(if $(filter 1,$(APPROVE)),--yes,)

eval: ## run the b2b-support eval suite: make eval [SUITE=...] [EXPORT=langfuse] [PROFILE=ollama]
	uv run harness eval $(or $(SUITE),examples/b2b-support/eval.yaml) $(if $(MODEL),--model $(MODEL),) $(if $(EXPORT),--export $(EXPORT),) $(if $(PROFILE),--profile $(PROFILE),)

up: gateway ## alias for `make gateway`

runs: ## list agent runs from the event log (make runs RUN=<id> drills in)
	uv run harness runs $(if $(RUN),--run $(RUN),)

logs: ## follow compose logs
	docker compose logs -f --tail=50

down: ## stop the compose stack
	docker compose down

clean: ## remove local run state (db, transcripts, caches) — keeps the venv
	rm -rf rigging.db rigging.db-wal rigging.db-shm runs/ \
	       .pytest_cache/ .mypy_cache/ .ruff_cache/ .coverage htmlcov/
