# AGENTS.md — Runway (AI Usage Tracker)

This file is the single source of truth for this repo's workflow rules and
load-bearing invariants — the ones every agent needs before touching
anything, regardless of which CLI you are. `CLAUDE.md` is a one-line
`@AGENTS.md` import, so Claude Code reads this file through it; Codex,
opencode, and friends read it natively. A stock Gemini CLI still looks for
`GEMINI.md` (removed here on purpose) — set `"context.fileName":
"AGENTS.md"` in `~/.gemini/settings.json` if yours needs it. For deeper
detail see
[`docs/architecture.md`](docs/architecture.md) (data model, collectors,
CI/CD), [`docs/collection_logic.md`](docs/collection_logic.md) (card merge
semantics), [`CONTRIBUTING.md`](CONTRIBUTING.md) (contributor workflow),
[`docs/SECURITY.md`](docs/SECURITY.md), and
[`docs/data-health.md`](docs/data-health.md).

## Workflow rules

- **Branch off the latest remote default branch, never off your local one.**
  `git fetch` updates `origin/*` but never fast-forwards local `main`, so a
  local `main` is routinely stale. Always:
  `git fetch origin && git checkout -b <branch> origin/main`.
  Branching off local `main` is what makes a PR show up as "out-of-date with
  the base branch" the moment it is opened (`main` requires *strict*,
  up-to-date status checks). (Exception: deliberately stacking on another
  feature branch.) Work in a dedicated git worktree per branch — this repo
  conventionally uses the gitignored `.wt/<slug>`.
- **Never commit directly to `main`.** Branch and open a PR.
- **PR title needs a Conventional Commits prefix** (`feat:`, `fix:`, `chore:`,
  ...). This repo squash-merges, so the PR title becomes the commit message on
  `main` — Release Please parses it to cut versions/changelogs, and an
  unprefixed title silently drops out of the changelog.
- **Before pushing, run the full gate:**
  `make lint && make test && make web-test`.
  `make install` wires the same checks into git hooks: pre-commit runs ruff,
  mypy, detect-secrets, and the unit tests; pre-push runs the full suite.
  CI must stay green — `test-python / lint`, `test-python / type-and-test`,
  `test-frontend / lint-and-test`, and CodeQL — with coverage floors of 70%
  (Python) and 85% (frontend).
- **Get a review, and close the loop on it.** Hermes auto-reviews each PR
  once on open (`.github/workflows/hermes.yml`) — don't request another one
  right away. After pushing fixes, ask for a re-review with a
  `@s3ntin3l8-hermes Review` comment on the PR, and keep that to a couple of
  rounds. Fixing the code is not enough to address feedback — reply to each
  inline comment, then resolve the thread:

  1. Reply:
     ```bash
     gh api repos/s3ntin3l8/runway-ai-usage-tracker/pulls/<PR>/comments/<comment_id>/replies -f body="Fixed in <commit>"
     ```
  2. Resolve (GraphQL-only concept):
     ```bash
     gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<thread_id>"}) { thread { isResolved } } }'
     ```
     Get `thread_id` from a `reviewThreads` query on the PR, not the REST
     comment id.
- **Run a review pass on your own diff before declaring done.**
- **File a GitHub issue for anything a plan defers, blocks, or descopes**, and
  link it from the PR — a footnote in a plan doc is not a durable record.
- **Post-merge cleanup:** `git branch -d <branch>`,
  `git push origin --delete <branch>`, and `git worktree remove <path>` if a
  worktree was used. Skipping this lets stale references accumulate.
- **Fresh clone or worktree:** run `make install` first (venv, Python + webapp
  dependencies, git hooks) — none of it is inherited.

## Everyday commands

`make help` lists them all; the ones you'll reach for:

- `make dev` / `make dev-all` — server with hot reload; or server + Vite
  (:5173) + sidecar together.
- `make test` — pytest; a single file: `pytest tests/path/to/test_file.py`.
- `make lint` / `make format` — ruff + mypy + pip-audit; auto-fix formatting.
- `make web-test` / `make web` — frontend vitest; production SPA build into
  `webapp/dist`.
- `make sidecar` — run the sidecar agent against the dev config dir.
- `make sidecar-registry` — regenerate the sidecar's baked credential rules after
  editing `app/core/registry.json` (the baked block is generated, never hand-edited).
- `make secrets` / `make secrets-baseline` — detect-secrets gate / regenerate
  the baseline after vetting a new detection.

## Core invariants

- **Event-sourced data model.** `usage_events` is authoritative; rollups,
  windows, gauges, and snapshots are derived views. See
  [`docs/architecture.md`](docs/architecture.md#data-model) for the table map,
  ingest path, and read/mutating endpoints. Never hand-patch a derived table —
  repairs go through `app/services/maintenance/` (plan/apply pairs) so the
  in-app fixer and the `scripts/*.py` wrappers cannot drift apart; see
  [`docs/data-health.md`](docs/data-health.md).
- **Two topologies; the server never does local detection.** Local
  (server + sidecar on one host) or multi-host/Docker: LSP probes, browser
  cookies, and IDE/file introspection all run in the sidecar. The server
  container has no native desktop UI or keychains — credentials arrive via
  ENV vars or sidecar payloads.
- **Dev and prod never share a data dir.** SQLite is single-writer; `make dev*`
  defaults `RUNWAY_CONFIG_DIR` to the gitignored `./data`.
- **Schema changes are forward-safe (no Alembic).** New columns are added by
  `init_db`'s idempotent `ALTER TABLE` list in `app/core/db.py`; an image
  bump must never break an existing DB.
- **Adding a card field** means updating `LimitCard` in
  `app/models/schemas.py`, the mirror in `webapp/src/api/types.ts`, and the
  interface example in [`docs/api-reference.md`](docs/api-reference.md).
- **Collector code declares its taxonomy** — `data_source` (api / web / local)
  and `input_source` (config / server / sidecar) — and anything `local` runs
  in the sidecar, never the server. See
  [`docs/architecture.md`](docs/architecture.md#collectors) and
  [`docs/collection_logic.md`](docs/collection_logic.md).
- **Branding:** `assets/logo.svg` is canonical; every other surface (favicon,
  PWA/tray/installer art) is derived. Run `make logo` to regenerate; never
  hand-edit a derived asset. See [`docs/branding.md`](docs/branding.md).
- **Style:** Python 3.12 + FastAPI + Pydantic v2 — `async` everywhere,
  explicit type hints, graceful "Error Card" degradation instead of crashes.
  Frontend is React 19 + TypeScript in `webapp/` using semantic token
  utilities (`bg-surface-1`, `text-fg-muted`), never raw hex. Sidecars only
  extract/forward raw data; the server does the heavy lifting.
