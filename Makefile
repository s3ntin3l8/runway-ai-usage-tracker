.DEFAULT_GOAL := help
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy

.PHONY: help install install-hooks dev run sidecar test test-cov lint format css watch secrets clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install-hooks: ## Wire up tracked .githooks/ as the git hooks directory
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/pre-push

install: install-hooks ## Set up venv, install Python and Node dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	npm ci

dev: ## Run development server (hot reload, port 8765)
	$(VENV)/bin/uvicorn app.main:app --reload --port 8765

run: ## Run production server
	$(PYTHON) -m app.main

sidecar: ## Run the sidecar agent
	$(PYTHON) scripts/sidecar.py

test: ## Run test suite (matches CI)
	$(PYTEST) --ignore=tests/unit/test_browser_cookies.py

test-cov: ## Run tests with coverage report
	$(PYTEST) --ignore=tests/unit/test_browser_cookies.py --cov=app --cov-report=term-missing

lint: ## Run all linters (ruff, mypy, pip-audit)
	$(RUFF) check . && $(RUFF) format --check .
	$(MYPY) .
	$(VENV)/bin/pip-audit -r requirements.txt

format: ## Auto-fix ruff lint and formatting issues
	$(RUFF) check --fix .
	$(RUFF) format .

css: ## Build Tailwind CSS (one-shot)
	npm run build:css

watch: ## Watch and rebuild Tailwind CSS on change
	npm run watch:css

secrets: ## Scan for secrets against baseline
	$(VENV)/bin/detect-secrets scan --baseline .secrets.baseline

clean: ## Remove venv, caches, and build artifacts
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml node_modules
