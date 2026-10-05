# Contributing

## Setup

```bash
make install    # venv + Python deps + webapp deps + git hooks (pre-commit & pre-push)
```

Then `make dev-all` (server + Vite + sidecar) or `make dev` (server only) —
note that `make dev` alone no longer sees your local CLI logins (Gemini, Claude,
Codex, `gh`, opencode, ...): the server reads no credential files, so use
`make dev-all` to collect from them. See the [Quick Start](README.md#quick-start) and
[Development Shortcuts](README.md#development-shortcuts) in `README.md`.

## Before opening a PR

The git hooks catch most problems first: `pre-commit` runs ruff, mypy,
detect-secrets, and the unit tests; `pre-push` additionally runs the full
suite. Run the same gate by hand:

```bash
make lint && make test && make web-test
```

- **Branch off `origin/main`**, never your local `main`, and keep the branch
  up to date — `main` requires strict status checks (see the workflow rules in
  [AGENTS.md](AGENTS.md)).
- **PR title must use a
  [Conventional Commits](https://www.conventionalcommits.org/) prefix**
  (`feat:`, `fix:`, `chore:`, `docs:`, ...). This repo squash-merges PRs and
  Release Please parses the **PR title**, not the individual commits, to cut
  versions/changelogs — an unprefixed title silently drops from the release.
- **CI must pass**: `test-python / lint`, `test-python / type-and-test`,
  `test-frontend / lint-and-test`, and CodeQL, with coverage floors of 70%
  (Python) and 85% (frontend).
- **Schema changes are forward-safe** (no Alembic): new columns go into
  `app/core/db.py`'s idempotent `ALTER TABLE` list so existing databases
  upgrade on startup.
- Describe *why* the change exists, not just what changed.

## Review process

Hermes reviews each PR once automatically when it opens; after pushing fixes,
request another look with a `@s3ntin3l8-hermes Review` comment. Addressing
review feedback (reply + resolve each inline thread via the GitHub API) and
post-merge cleanup (delete local and remote branch, remove the worktree) are
documented in [AGENTS.md](AGENTS.md) — follow them rather than improvising.

## Project docs

- Architecture & data model: [docs/architecture.md](docs/architecture.md)
- Collection/merge logic: [docs/collection_logic.md](docs/collection_logic.md)
- Testing guide: [tests/README.md](tests/README.md)
- Security model: [docs/SECURITY.md](docs/SECURITY.md)
