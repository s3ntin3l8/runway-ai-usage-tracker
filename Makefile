.DEFAULT_GOAL := help
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy

.PHONY: help install install-hooks dev dev-all run sidecar test test-cov lint format web web-dev web-test secrets secrets-baseline clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install-hooks: ## Install pre-commit hooks for commit and push stages
	$(VENV)/bin/pre-commit install
	$(VENV)/bin/pre-commit install --hook-type pre-push

install: install-hooks ## Set up venv, install Python and Node dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	npm --prefix webapp ci

dev: ## Run development server (hot reload, port 8765)
	set -a; [ -f .env ] && . ./.env; set +a; \
	$(VENV)/bin/uvicorn app.main:app --reload \
	  --host "$${APP_HOST:-127.0.0.1}" \
	  --port "$${APP_PORT:-8765}"

dev-all: ## Run dev server and sidecar together (Ctrl-C stops both)
	$(MAKE) -j2 dev sidecar

run: ## Run production server
	$(PYTHON) -m app.main

sidecar: ## Run the sidecar agent (sources .env so RUNWAY_CONFIG_DIR + INGEST_API_KEY align with the dev server)
	set -a; [ -f .env ] && . ./.env; set +a; \
	$(PYTHON) scripts/sidecar.py

test: ## Run test suite (matches CI)
	$(PYTEST)

test-cov: ## Run tests with coverage report
	$(PYTEST) --cov=app --cov-report=term-missing

lint: ## Run all linters (ruff, mypy, pip-audit)
	$(RUFF) check . && $(RUFF) format --check .
	$(MYPY) .
	$(VENV)/bin/pip-audit -r requirements.txt

format: ## Auto-fix ruff lint and formatting issues
	$(RUFF) check --fix .
	$(RUFF) format .

web: ## Build the SPA (webapp/dist — what `make run` serves)
	npm --prefix webapp run build

web-dev: ## Run the Vite dev server (proxies /api to the dev server)
	npm --prefix webapp run dev

web-test: ## Run frontend unit tests (vitest)
	npm --prefix webapp test

secrets: ## Gate: fail if any tracked file has a secret not in the baseline (matches CI)
	git ls-files -z | xargs -0 $(VENV)/bin/detect-secrets-hook --baseline .secrets.baseline

secrets-baseline: ## Regenerate/update .secrets.baseline (run after vetting new detections, then commit)
	$(VENV)/bin/detect-secrets scan --baseline .secrets.baseline

clean: ## Remove venv, caches, and build artifacts
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml node_modules webapp/node_modules webapp/dist
