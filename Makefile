# rigging — see docs/milestones.md for what each target depends on.
.PHONY: install test lint typecheck demo hello gateway observability up down clean

install:        ## create the venv and install dev extras
	uv sync --extra dev

test:           ## unit + wire fixtures + replay integration (no network)
	uv run pytest -m "not live"

lint:
	uv run ruff check .

typecheck:
	uv run mypy

check: lint typecheck test  ## what CI runs

gateway:        ## OpenAI-compatible gateway on :20128 (OmniRoute)
	docker compose up -d omniroute

observability:  ## gateway + Langfuse + Postgres + Qdrant
	docker compose --profile observability --profile retrieval up -d

hello:          ## v0.1 smoke test, no tools
	uv run harness run examples/hello/agent.yaml "say hello in one sentence"

demo:           ## the b2b-support demo agent (v0.3)
	uv run harness run examples/b2b-support/agent.yaml "what is the return window on SKU-1043?"

up: gateway
down:
	docker compose down
clean:
	rm -rf rigging.db rigging.db-wal rigging.db-shm runs/ .pytest_cache/ .mypy_cache/ .ruff_cache/
