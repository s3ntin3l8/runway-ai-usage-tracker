# Contributing

## Setup

```bash
make install    # venv + Python deps + webapp deps + git hooks (pre-commit & pre-push)
```

On native Windows, install Python 3.12+ and Node.js 22.22.2+ (including npm 11.11+).
`winget install OpenJS.NodeJS.LTS` puts Node in the per-user PATH, and that
only applies to *new* processes — start a fresh shell (or a fresh agent
session) before `node`/`npm` resolve. Then run these commands in PowerShell
from the repository root; Make and WSL are not required:

```powershell
python scripts/dev_checks.py install
python scripts/dev_checks.py gate
```

Individual commands are `lint`, `test`, `web-test`, and `install-hooks`; the
same runner also works on Linux and macOS. It uses this checkout's `.venv`,
sets UTF-8 mode for Python child processes, installs the lockfile dependency
graph with `npm ci`, and gives pytest fresh owned home, config, app-data and
temporary directories so discovery fixtures cannot touch developer logins.
For example: `python scripts/dev_checks.py test tests/unit/test_self_update.py`.
`install-python` is the Python-only half of `install` — no npm, no git hooks —
which is what the native Windows CI jobs use.
Git Bash is needed for the registry-login shell action's tests on Windows.
Symlink tests require Windows Developer Mode or an elevated process; a missing
symlink privilege is reported as a specific skip, not Windows acceptance of those checks.
POSIX permission assertions remain active on POSIX; Windows still checks
credential contents and temporary-file cleanup. Frontend English date-label tests
use a fixed test locale, while the dashboard retains the user's locale.

The frontend toolchain is pinned. `webapp/package.json` declares
`packageManager: npm@11.19.0` and a `devEngines` block requiring Node 22.22.2+
and npm 11.11+, so npm refuses to install with a node or npm outside that
range instead of quietly producing a different lockfile. Node 22.22.2 is the
floor for the `jsdom` 30 upgrade — it pins
`^22.22.2 || ^24.15.0 || >=26.0.0`, which is also the intersection with
vitest 5's `^22.12.0 || ^24.0.0 || >=26.0.0` and `react-router`'s
`>=22.22.0` — so the floor is raised ahead of that bump, and it stays correct
whether or not the bump has landed. npm 11.11 is the floor
because npm 10 never writes the `libc` metadata described below and strips it
from every lockfile it rewrites, and npm 11.11 is the first release that
writes it. Node 24 ships npm 11.19, so `winget install OpenJS.NodeJS.LTS` is
the simplest route. Add `--force` to an npm command to downgrade a
`devEngines` mismatch to a warning.

The pin lives in `webapp/package.json` and not in the repository root: the
root manifest is a script wrapper with no dependencies and no lockfile, and
`actions/setup-node` v7 enables its npm cache whenever the manifest at the
repository root names npm as the package manager - it then looks for a
`package-lock.json` there and fails every workflow that sets up Node.

The `libc` field is worth understanding before you regenerate the lockfile.
npm copies it verbatim from an optional platform package's published manifest,
and it is the only thing that tells npm whether to install the glibc or the
musl build of `sharp`, `rollup` and `rolldown` - without it npm installs both,
which grew `node_modules/@img` from 19M to 37M on a glibc host. npm 10 does
not write the field at all, so `npm install --package-lock-only` under npm 10
silently removes it from all 35 platform entries; npm 11.11 and later write
it. That is why the npm version is pinned rather than left to whatever happens
to be installed. `tests/unit/test_webapp_lockfile.py` fails if the field goes
missing.

The native Windows CI job runs the suite as four parallel shards. The same
split is available locally:

```bash
make test-shard SHARD=1 SHARDS=4   # one shard
python scripts/pytest_shard.py plan # show the whole split
```

A shard owns whole test modules, never part of one. The plan is balanced by
measured per-module runtime (`tests/shard_durations.<platform>.json`, one table
per platform), so re-measure it with `make shard-weights` after the suite's shape
changes materially - that command runs the full suite once and rewrites the
table for the platform it ran on. The Windows table is what the native Windows
shards are planned with; measure it on Windows, not on Linux.

Git hooks live in the repository's shared `.git/hooks`, so every worktree of
this repository writes into the same two hook files. Install them once from the
main checkout with `make install-hooks`: a hook installed from a worktree keeps
pointing at that worktree's interpreter and stops working once the worktree is
removed. A worktree that commits also needs its own `.venv`, because the mypy
and pytest hooks resolve the interpreter from the checkout being committed.
`make install` provides one; linking the main checkout's `.venv` into the
worktree is enough, because the runner takes the repository root from its own
path and runs the tests with that root as the working directory, so the
checkout's own sources are the ones under test. The pre-push hook runs the
full suite in four workers, so allow it a minute or so.

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
make lint && make test && make web-lint && make web-test
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
  `test-frontend / lint-and-test`, `dependency-review / dependency-review`,
  and CodeQL, with coverage floors of 85%
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


Remote development: Vite defaults to `127.0.0.1`; set `VITE_HOST` explicitly
when remote access is needed. Configure the API in network mode with an admin
key, explicit CORS origins and TLS (see `docs/SECURITY.md`). A remote browser
cannot use the API's localhost administrator bypass through Vite.
