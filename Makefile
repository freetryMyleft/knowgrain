.PHONY: configure sync db-up db-status db-down migrate models tokenizer api demo test web-sync web web-build

configure:
	@test -f .env || cp .env.example .env

sync:
	uv sync --locked

db-up: configure
	docker compose --env-file .env -f infra/compose/compose.yaml up -d postgres

db-status:
	docker compose --env-file .env -f infra/compose/compose.yaml ps

db-down:
	docker compose --env-file .env -f infra/compose/compose.yaml down

LLM_MODEL ?= qwen3.6:35b
EMBEDDING_MODEL ?= qwen3-embedding:0.6b

models:
	ollama pull "$(LLM_MODEL)"
	ollama pull "$(EMBEDDING_MODEL)"

tokenizer:
	uv run knowgrain-tokenizer

api:
	uv run knowgrain-api

migrate:
	uv run alembic upgrade head

test:
	uv run python -m unittest discover -s tests -v

demo:
	uv run knowgrain

web-sync:
	npm ci --prefix apps/web

web:
	npm run dev --prefix apps/web -- --host 127.0.0.1

web-build:
	npm run build --prefix apps/web
