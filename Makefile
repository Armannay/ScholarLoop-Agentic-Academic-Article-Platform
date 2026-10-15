# ScholarLoop - developer entry points. One place to edit for the whole team.
SHELL := /bin/bash
VENV  := .venv
PY    := $(VENV)/bin/python
PIP   := $(VENV)/bin/pip

.DEFAULT_GOAL := help

.PHONY: help venv install lint format type test check schemas schemas-check run backend frontend-install frontend-build frontend-dev dev up down clean

help: ## show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/activate: pyproject.toml
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

venv: $(VENV)/bin/activate ## create the virtualenv

install: venv ## install backend deps (pinned in pyproject.toml) into .venv
	$(PIP) install --quiet -e ".[dev]"

lint: ## ruff check + ruff format --check
	$(VENV)/bin/ruff check backend
	$(VENV)/bin/ruff format --check backend

format: ## apply ruff fixes and formatting
	$(VENV)/bin/ruff check --fix backend
	$(VENV)/bin/ruff format backend

type: ## mypy --strict on backend/app
	$(VENV)/bin/mypy

test: ## pytest (MockProvider only - never live APIs)
	$(PY) -m pytest

check: lint type test ## the gate every step must pass

schemas: ## export contracts JSON Schemas to docs/schemas/
	$(PY) -m app.scripts.export_schemas --out docs/schemas

schemas-check: ## fail if docs/schemas is out of date with the contracts
	$(PY) -m app.scripts.export_schemas --out docs/schemas --check

run: ## run the API on :8000 (MockProvider only)
	$(VENV)/bin/uvicorn app.api.main:app --app-dir backend --host 0.0.0.0 --port 8000

backend: run ## alias for run

frontend-install: ## npm install for frontend/
	cd frontend && npm install

frontend-build: ## typecheck + production build of frontend/
	cd frontend && npm run build

frontend-dev: ## run the Vite dev server on :5173 (binds 0.0.0.0)
	cd frontend && npm run dev

dev: ## hint for running both halves in two terminals
	@echo "terminal 1: make run          # API on :8000"
	@echo "terminal 2: make frontend-dev # UI  on :5173 (proxies /api -> :8000)"

up: ## docker compose up
	docker compose up --build

down: ## docker compose down
	docker compose down -v

clean: ## remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache backend/**/__pycache__ frontend/dist
