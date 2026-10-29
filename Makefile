# rigging — everyday commands. `make` on its own prints this list.
#
# Choices you can override on the command line:
#   MANIFEST=examples/hello/agent.yaml     which agent to run
#   PROMPT="..."                           the user message
#   MODEL=qwen3:8b                         override the manifest's model
#   APPROVE=0|1                            allow side-effecting tools without asking (demo only)
#   DB=other.db                            alternate SQLite event log
#
# Gateway options (pick one, both are OpenAI-compatible HTTP):
#   make gateway        OmniRoute in Docker on :20128 — 375 providers, one endpoint
#   ollama signin       host Ollama on :11434 — no Docker, needs `beam:cloud` or a pulled model
#   RIGGING_BASE_URL=<any /v1 URL>   vLLM, LM Studio, OpenRouter, a friend's proxy

.DEFAULT_GOAL := help
SHELL := /bin/bash

MANIFEST ?= examples/hello/agent.yaml
PROMPT   ?= say hello in one sentence
MODEL    ?=
HELLO_MODEL ?= freellm/codestral   # proven live through OmniRoute -> freellmapi
DB       ?=
APPROVE  ?= 0

.DELETE_ON_ERROR:
.PHONY: help install check test lint typecheck format gateway observability \
        retrieval hello demo run chat index eval up down logs clean

help: ## show this help
	@printf '\nrigging — modular agent harness\n\n'
	@printf 'Usage: make <target> [MANIFEST=... PROMPT="..." MODEL=...]\n\n'
	@printf 'Targets:\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nSetup:\n'
	@printf '  uv sync --extra dev      core + test/lint tooling\n'
	@printf '  uv sync --extra qdrant   retrieval (v0.3)   [also: laya, langfuse, mcp, service]\n'
	@printf '\nModel access (one is enough):\n'
	@printf '  ollama signin            beam:cloud, free at time of writing, no API key\n'
	@printf '  ollama pull qwen3:8b     fully local, the weakest model the demo must pass on\n'
	@printf '  RIGGING_PROFILE_OPENROUTER_API_KEY=...   in .env, for better quality\n'
	@printf '\nCompose profiles:\n'
	@printf '  (default)      omniroute only — smallest footprint, enough to run an agent\n'
	@printf '  retrieval      + qdrant on :6345 (6333/6335/6340 are taken by other stacks)\n'
	@printf '  observability  + langfuse and postgres on :3000, for traces\n'
	@printf '\nFirst run: make install && make gateway && make hello\n\n'

install: ## create the venv with dev extras
	uv sync --extra dev

check: ## what CI runs: lint + typecheck + tests (no network)
	$(MAKE) --no-print-directory lint typecheck test

test: ## unit + SSE wire fixtures + loop invariants (never touches the network)
	uv run pytest -m "not live"

test-live: ## opt-in: hit a real gateway (needs `ollama signin` or a pulled model)
	uv run pytest -m live

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

observability: ## gateway + Qdrant + Langfuse (:3000) + Postgres (v0.4 traces)
	docker compose --profile retrieval --profile observability up -d
	@printf 'langfuse: http://localhost:3000  (create an account, then set RIGGING_OTLP_* in .env)\n'

hello: ## v0.1 smoke test: one prompt, no tools (proven end-to-end; see examples/hello/)
	RIGGING_BASE_URL=$${RIGGING_BASE_URL:-http://localhost:20128/v1} \
	uv run harness run $(MANIFEST) "$(PROMPT)" --model $(if $(MODEL),$(MODEL),$(HELLO_MODEL))

run: ## run any agent: make run MANIFEST=examples/b2b-support/agent.yaml PROMPT="..."
	uv run harness run $(MANIFEST) "$(PROMPT)" $(if $(MODEL),--model $(MODEL),) \
	  $(if $(DB),--db $(DB),) $(if $(filter 1,$(APPROVE)),--yes,)

chat: ## interactive REPL: make chat MANIFEST=examples/hello/agent.yaml
	uv run harness chat $(MANIFEST) $(if $(DB),--db $(DB),) $(if $(SESSION),--session $(SESSION),)

seed: ## build the demo fixture DB: products, orders, policy chunks (deterministic)
	uv run python examples/b2b-support/seed.py

index: ## (re)build the retrieval index: no-op for fts5 (seed does it), embeds for qdrant
	uv run harness index $(MANIFEST)

eval: ## run an eval suite over the event log (v0.4)
	uv run harness eval

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
