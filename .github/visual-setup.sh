#!/usr/bin/env bash
# What the demo needs on a clean runner (called by the shared visual workflows, after they installed Python
# and Node). `make demo` runs from the repo's .venv and builds webapp/dist itself.
set -euo pipefail

python -m venv .venv
.venv/bin/pip install --quiet -r requirements-dev.txt
npm --prefix webapp ci --no-audit --no-fund
