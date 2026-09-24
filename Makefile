.DEFAULT_GOAL := help
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy

# Source .env (if present) into the recipe shell, exporting every var.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a

.PHONY: help install install-hooks dev dev-all run run-all sidecar sidecar-app sidecar-dmg sidecar-installer test test-cov lint format web web-dev web-test logo secrets secrets-baseline clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install-hooks: ## Install pre-commit hooks for commit and push stages
	$(VENV)/bin/pre-commit install
	$(VENV)/bin/pre-commit install --hook-type pre-push

install: install-hooks ## Set up venv, install Python and Node dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	npm --prefix webapp ci

dev: ## Run development server (hot reload). Data → ./data (gitignored) unless RUNWAY_CONFIG_DIR is set.
	$(LOAD_ENV); \
	RUNWAY_CONFIG_DIR="$${RUNWAY_CONFIG_DIR:-$(CURDIR)/data}" \
	$(VENV)/bin/uvicorn app.main:app --reload \
	  --host "$${APP_HOST:-127.0.0.1}" \
	  --port "$${APP_PORT:-8765}"

dev-all: ## Run the full dev stack — server + Vite frontend (:5173) + sidecar (Ctrl-C stops all)
	$(MAKE) -j3 dev web-dev sidecar

run: ## Run production server (serves the built SPA from webapp/dist at :8765)
	$(PYTHON) -m app.main

run-all: web ## Build the SPA, then run the production server + sidecar (no hot reload)
	$(MAKE) -j2 run sidecar

sidecar: ## Run the sidecar agent (config → ./data to match `make dev`; override with RUNWAY_CONFIG_DIR)
	$(LOAD_ENV); \
	RUNWAY_CONFIG_DIR="$${RUNWAY_CONFIG_DIR:-$(CURDIR)/data}" \
	$(PYTHON) scripts/sidecar.py

# --- Sidecar packaging (CI does the same in .github/workflows/sidecar-build.yml) ---
SIDECAR_APP := dist/Runway Sidecar.app

sidecar-app: ## macOS only: build + ad-hoc sign dist/Runway Sidecar.app (needs pyinstaller in the venv)
	@[ "$$(uname)" = "Darwin" ] || { echo "sidecar-app needs macOS"; exit 1; }
	$(VENV)/bin/pyinstaller --noconfirm sidecar_app/spec/macos.spec
	codesign --force --deep --sign - "$(SIDECAR_APP)"
	codesign --verify --strict --verbose=2 "$(SIDECAR_APP)"

sidecar-dmg: sidecar-app ## macOS only: package the drag-to-Applications DMG (brew install create-dmg)
	rm -rf dist/dmg-root dist/Runway-Sidecar-macOS-dev.dmg
	mkdir -p dist/dmg-root
	cp -R "$(SIDECAR_APP)" dist/dmg-root/
	tiffutil -cathidpicheck installer/assets/dmg-background.png installer/assets/dmg-background@2x.png -out dist/dmg-background.tiff
	create-dmg --volname "Runway Sidecar" --volicon installer/assets/app.icns \
		--background dist/dmg-background.tiff --window-pos 200 120 --window-size 760 480 \
		--icon-size 128 --icon "Runway Sidecar.app" 180 320 --hide-extension "Runway Sidecar.app" \
		--app-drop-link 580 320 --no-internet-enable dist/Runway-Sidecar-macOS-dev.dmg dist/dmg-root/

sidecar-installer: ## Compile the Windows NSIS installer from dist/RunwaySidecar.exe (needs makensis; works on Linux/macOS too)
	@[ -f dist/RunwaySidecar.exe ] || { echo "dist/RunwaySidecar.exe missing — build it on Windows with pyinstaller sidecar_app/spec/windows.spec"; exit 1; }
	eval "$$($(PYTHON) sidecar_app/spec/win_version.py)"; \
	makensis -V2 -DVERSION="$$VERSION" -DPRODUCT_VERSION_QUAD="$$PRODUCT_VERSION_QUAD" installer/windows/runway-sidecar.nsi
	@echo "→ dist/runway-sidecar-setup.exe"

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

web: ## Build the SPA for production into webapp/dist (served by `make run`/`make run-all` at :8765)
	npm --prefix webapp run build

web-dev: ## Live Vite dev server on :5173 with HMR (sources .env so RUNWAY_API_URL/VITE_PORT apply)
	$(LOAD_ENV); \
	npm --prefix webapp run dev

web-test: ## Run frontend unit tests (vitest)
	npm --prefix webapp test

logo: ## Regenerate every brand surface from the canonical assets/logo.svg (see docs/branding.md)
	cp assets/logo.svg webapp/public/favicon.svg
	npm --prefix webapp run generate-pwa-assets
	$(PYTHON) sidecar_app/assets/generate_icons.py
	npm --prefix webapp run render-brand-rasters
	$(PYTHON) installer/generate_app_icons.py

secrets: ## Gate: fail if any tracked file has a secret not in the baseline (matches CI)
	git ls-files -z | xargs -0 $(VENV)/bin/detect-secrets-hook --baseline .secrets.baseline

secrets-baseline: ## Regenerate/update .secrets.baseline (run after vetting new detections, then commit)
	$(VENV)/bin/detect-secrets scan --baseline .secrets.baseline

clean: ## Remove venv, caches, and build artifacts
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml node_modules webapp/node_modules webapp/dist
