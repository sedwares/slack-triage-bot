.PHONY: help up down migrate seed bootstrap reset phase0 run test lint fmt psql check-secrets

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up:              ## Start local Postgres and wait for readiness
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U triage -d triage >/dev/null 2>&1; \
	  do sleep 1; done
	@echo "postgres ready on localhost:5433"

down:            ## Stop Postgres (keeps the volume)
	docker compose down

migrate:         ## Apply pending migrations
	python scripts/migrate.py up

seed:            ## Load reference data (model pricing)
	python scripts/migrate.py seed

bootstrap:       ## Configure a workspace/channel for local dev (see --help)
	python scripts/bootstrap_dev.py --show

reset:           ## Destroy and rebuild the local database. Irreversible.
	docker compose down -v
	$(MAKE) up migrate seed

phase0:          ## Phase 0 — verify Slack tokens with the echo bot
	python phase0/echo_bot.py

run:             ## Phase 1 — run the triage bot
	python -m triage.main

test:
	pytest -q

lint:
	ruff check src tests phase0 scripts
	mypy src

fmt:
	ruff format src tests phase0 scripts
	ruff check --fix src tests phase0 scripts

psql:            ## Open a psql shell against local Postgres
	docker compose exec postgres psql -U triage -d triage

check-secrets:   ## Fail if anything token-shaped is staged. Wire into pre-commit.
	@# Excludes .env.example (its placeholders are intentional) and any literal
	@# REPLACE-ME value. Without both exclusions this fires on every commit, and a
	@# check that always fails is a check everyone learns to bypass with --no-verify.
	@if git diff --cached -U0 -- . ':(exclude).env.example' \
	  | grep -nE '^\+' \
	  | grep -vE 'REPLACE-ME' \
	  | grep -nE '(xoxb-|xapp-|xoxp-|xoxa-|sk-ant-)[A-Za-z0-9_-]{20,}'; then \
	  echo "REFUSING: token-shaped string found in staged changes."; exit 1; \
	else echo "no token-shaped strings staged"; fi
