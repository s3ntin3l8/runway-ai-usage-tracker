.DEFAULT_GOAL := help
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy

# Source .env (if present) into the recipe shell, exporting every var.
LOAD_ENV := set -a; [ -f .env ] && . ./.env; set +a

.PHONY: help install install-hooks remove-worktree demo screenshots screenshots-landing screenshots-audit visual-check visual-baseline dev dev-all run run-all sidecar sidecar-registry sidecar-app sidecar-dmg sidecar-installer test test-unit test-shard test-cov shard-weights lint format web web-dev web-lint web-test logo secrets secrets-baseline clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install-hooks: ## Install pre-commit hooks for commit and push stages
	python3 scripts/dev_checks.py install-hooks

remove-worktree: ## Remove a worktree without deleting the directories it links to (use: make remove-worktree PATH=.wt/<slug>)
	python3 scripts/dev_checks.py remove-worktree $(PATH)

install: ## Set up venv, install Python and Node dependencies
	python3 scripts/dev_checks.py install

demo: ## Throwaway isolated demo with synthetic data (scratch DB/HOME, clean env, no .env, loopback :3965); Ctrl-C deletes it
	$(PYTHON) -m scripts.demo.instance

# Visual pipeline (s3ntin3l8/visual-kit): README and landing screenshots, audit evidence and the per-change
# visual check, all rendered in one pinned image against `make demo`. The image is pinned in visual/Dockerfile
# (Dependabot bumps it); visual/.kit/run.sh is extracted from it. Spec: visual-kit's CONTRACT.md.
VISUAL_IMAGE := visual-kit-local/runway
VISUAL_RUN := VISUAL_KIT_IMAGE=$(VISUAL_IMAGE) visual/.kit/run.sh

visual/.kit/run.sh: visual/Dockerfile
	docker build -q -t $(VISUAL_IMAGE) - < visual/Dockerfile
	mkdir -p visual/.kit && docker run --rm $(VISUAL_IMAGE) host-files | tar -x -C visual/.kit

screenshots: visual/.kit/run.sh ## README screenshots into docs/screenshots (committed) and the PWA manifest copies (webapp/public/screenshots). FORCE=1 replaces a folder visual-kit did not write
	$(VISUAL_RUN) readme $(if $(FORCE),--force)
	@# The README shows stills only; the loop is for the landing page (screenshots-landing). Loops re-encode
	@# differently on every run, so keeping them here would make the weekly refresh PR itself noise.
	rm -f docs/screenshots/*.webm docs/screenshots/*.mp4 docs/screenshots/*.jpg
	node scripts/sync-pwa-screenshots.mjs

screenshots-landing: visual/.kit/run.sh ## Landing-page stills and loops into OUT (default ./landing-shots) for personal-landing
	$(VISUAL_RUN) landing --out "$(or $(OUT),landing-shots)" $(if $(FORCE),--force)

screenshots-audit: visual/.kit/run.sh ## Audit evidence for AUDIT=YYYY-MM-<scope>-<topic> into docs/audit/assets/ (local only)
	@test -n "$(AUDIT)" || { echo "AUDIT=YYYY-MM-<scope>-<topic> is required"; exit 1; }
	$(VISUAL_RUN) audit --slug "$(AUDIT)" $(if $(FORCE),--force)

# Only a whole number from 1 to 99 is forwarded. REPEAT_OK is a pinned mirror of the range visual-kit's launcher
# accepts (host/run.sh, 0.4.0): if the launcher's range moves, move this list with it. An ambient REPEAT=yes, 0 or
# 100 in the environment is ignored with a warning, as the launcher does.
REPEAT_OK := 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71 72 73 74 75 76 77 78 79 80 81 82 83 84 85 86 87 88 89 90 91 92 93 94 95 96 97 98 99
REPEAT_N := $(and $(REPEAT),$(if $(word 2,$(strip $(REPEAT))),,$(filter $(REPEAT_OK),$(strip $(REPEAT)))))
# An invalid REPEAT given on the command line is an error, before the image build: a typo must not look like a determinism check that ran once.
ifneq ($(filter visual-check,$(MAKECMDGOALS)),)
ifneq ($(strip $(REPEAT)),)
ifeq ($(REPEAT_N),)
ifeq ($(origin REPEAT),command line)
$(error REPEAT must be a whole number from 1 to 99, got "$(REPEAT)")
else
$(warning REPEAT="$(REPEAT)" from the environment is ignored: not a whole number from 1 to 99, so no determinism check runs)
endif
endif
endif
endif

visual-check: visual/.kit/run.sh ## Before/after of the routes a UI change touches (ROUTES=/history,provider) against BASE (default: merge base); REPEAT=N also recaptures the head N-1 times and fails if it is not deterministic
	$(VISUAL_RUN) check $(if $(ROUTES),--routes "$(ROUTES)") $(if $(BASE),--base "$(BASE)") $(if $(FORCE),--force) $(if $(REPEAT_N),--repeat "$(REPEAT_N)")

visual-baseline: visual/.kit/run.sh ## Regenerate the committed baselines (visual/baselines): the scenes flagged "baseline": true
	$(VISUAL_RUN) baseline $(if $(FORCE),--force)

dev: ## Run development server (hot reload). Data → ./data (gitignored) unless RUNWAY_CONFIG_DIR is set.
	$(LOAD_ENV); \
	RUNWAY_CONFIG_DIR="$${RUNWAY_CONFIG_DIR:-$(CURDIR)/data}" \
	$(VENV)/bin/uvicorn app.main:app --reload --no-proxy-headers \
	  --host "$${APP_HOST:-127.0.0.1}" \
	  --port "$${APP_PORT:-8765}"

dev-all: ## Run the full dev stack — server + Vite frontend (:5173) + sidecar (Ctrl-C stops all)
	$(MAKE) -j3 dev web-dev sidecar

run: ## Run production server (serves the built SPA from webapp/dist at :8765)
	$(LOAD_ENV); $(PYTHON) -m app.main

run-all: web ## Build the SPA, then run the production server + sidecar (no hot reload)
	$(MAKE) -j2 run sidecar

sidecar-registry: ## Regenerate the sidecar's baked credential registry from app/core/registry.json
	$(PYTHON) scripts/gen_sidecar_registry.py

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
	$(PYTHON) -m scripts.build_windows_installer --from-package-version --out-dir dist

test: ## Run test suite (matches CI); JOBS=N parallel workers, JOBS=1 serial
	$(PYTHON) scripts/dev_checks.py test --jobs $(or $(JOBS),4)

test-unit: ## Run the unit tests only (what the pre-commit hook runs); JOBS=N workers
	$(PYTHON) scripts/dev_checks.py unit --jobs $(or $(JOBS),4)

test-shard: ## Run one shard of the suite (SHARD=1 SHARDS=4), as the Windows CI does
	$(PYTHON) scripts/dev_checks.py test-shard --shard-id $(or $(SHARD),1) --shards $(or $(SHARDS),4)

shard-weights: ## Re-measure per-test-module durations for the CI shard plan
	$(PYTHON) scripts/dev_checks.py record-shard-weights

test-cov: ## Run tests with coverage report
	$(PYTEST) --cov=app --cov-report=term-missing

lint: ## Run all linters (ruff, mypy, pip-audit)
	$(PYTHON) scripts/dev_checks.py lint

format: ## Auto-fix ruff lint and formatting issues
	$(RUFF) check --fix .
	$(RUFF) format .

web: ## Build the SPA for production into webapp/dist (served by `make run`/`make run-all` at :8765)
	npm --prefix webapp run build

web-dev: ## Live Vite dev server on :5173 with HMR (sources .env so RUNWAY_API_URL/VITE_PORT apply)
	$(LOAD_ENV); \
	npm --prefix webapp run dev

web-lint: ## Lint the frontend (eslint)
	$(PYTHON) scripts/dev_checks.py web-lint

web-test: ## Run frontend unit tests (vitest)
	$(PYTHON) scripts/dev_checks.py web-test

logo: ## Regenerate every brand surface from the canonical assets/logo.svg (see docs/branding.md)
	cp assets/logo.svg webapp/public/favicon.svg
	npm --prefix webapp run generate-pwa-assets
	$(PYTHON) sidecar_app/assets/generate_icons.py
	npm --prefix webapp run render-brand-rasters
	$(PYTHON) installer/generate_app_icons.py

secrets: ## Gate: fail if any tracked file has a secret not in the baseline (matches CI)
	git ls-files -z | xargs -0 $(PYTHON) scripts/check_secrets.py --baseline .secrets.baseline

secrets-baseline: ## Regenerate/update .secrets.baseline (run after vetting new detections, then commit)
	$(VENV)/bin/detect-secrets scan --baseline .secrets.baseline

clean: ## Remove venv, caches, and build artifacts
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml node_modules webapp/node_modules webapp/dist
