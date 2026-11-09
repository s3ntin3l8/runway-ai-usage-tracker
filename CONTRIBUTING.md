# Contributing

Process rules (branches, worktrees, PR titles, review, merge, cleanup) live in
[AGENTS.md](AGENTS.md); this file covers setup, the local gate and the
toolchain details a contributor needs.

## Setup

```bash
make install    # venv + Python deps + webapp deps + git hooks (pre-commit & pre-push)
```

Then `make dev-all` (server + Vite + sidecar) or `make dev` (server only).
`make dev` alone does not see your local CLI logins (Gemini, Claude, Codex,
`gh`, opencode, ...) because the server reads no credential files; use
`make dev-all` to collect from them. See the [Quick start](README.md#quick-start)
and run `make help` for every target.

A fresh clone or worktree inherits nothing, so run `make install` in each one.

### Frontend toolchain

The frontend needs Node 22.22.2+ and npm 11.11+. `webapp/package.json` declares
`packageManager` and a `devEngines` block (Node `^22.22.2 || ^24.15.0 || >=26.0.0`,
npm 11.11+), so npm refuses to install outside that range instead of quietly
producing a different lockfile; `--force` downgrades the mismatch to a warning.
Node 23.x and 24.0-24.14 are rejected. Node 24 ships a suitable npm.

The pin lives in `webapp/package.json`, not the repository root: the root
manifest is a script wrapper with no lockfile, and `actions/setup-node` enables
its npm cache whenever the root manifest names npm, then fails looking for a
root `package-lock.json`.

npm 11.11 is the floor because npm 10 does not write the `libc` metadata that
tells npm whether to install the glibc or musl build of platform packages
(`sharp`, `rollup`, `rolldown`, ...) and strips it from every lockfile it
rewrites. `tests/unit/test_webapp_lockfile.py` fails if the field goes missing.

### Native Windows

Install Python 3.12+ and Node.js 22.22.2+ (`winget install OpenJS.NodeJS.LTS`;
open a fresh shell afterwards so `node` resolves). Make and WSL are not
required; from the repository root in PowerShell:

```powershell
python scripts/dev_checks.py install
python scripts/dev_checks.py gate
```

`scripts/dev_checks.py` works on Linux and macOS too. Its subcommands are
`lint`, `mypy`, `unit`, `test`, `web-lint`, `web-test`, `install`,
`install-hooks`, `install-python`, `gate`, `test-shard`, `record-shard-weights`,
`test-windows`, `resolve-generated` and `remove-worktree`. It uses this
checkout's `.venv`, sets UTF-8 mode, installs the lockfile graph with `npm ci`,
and gives pytest fresh home, config and temp directories so tests cannot touch
developer logins. `install-python` is the Python-only half of `install`, used by
the native Windows CI jobs. Symlink tests need Developer Mode or an elevated
process; without it they skip with a specific message.

Two Windows-specific test traps:

- A local HTTP server that rejects a request on its headers alone answers and
  closes without reading the body; on Windows that close is a TCP reset and the
  client sees `ConnectionAbortedError` (WinError 10053) instead of the status.
  Tests that expect such a rejection send no payload (`Content-Length: 0`).
- `WinError 10048` in `tests/windows_socketpair.py` means the dynamic port range
  is crowded, typically by an IP Helper `portproxy` rule that forwards a port to
  itself (`netsh interface portproxy show v4tov4`). The helper retries, but the
  rule should be deleted.

### Sharded tests

Native Windows CI runs the suite as four shards, which you can reproduce:

```bash
make test-shard SHARD=1 SHARDS=4   # one shard
python scripts/pytest_shard.py plan # show the whole split
```

A shard owns whole test modules and is balanced by per-platform durations in
`tests/shard_durations.<platform>.json`; re-measure with `make shard-weights`
after the suite's shape changes. On a conflicted rebase or merge of generated
files, run `make resolve-generated` (see [AGENTS.md](AGENTS.md)).

### Git hooks

`make install-hooks` copies `scripts/hooks/git-hook.sh` into the shared
`.git/hooks`, so every worktree uses the same two hook files; re-run it after
the tracked script changes. The hook resolves its interpreter when it runs, and
each worktree that commits needs its own `.venv` (`make install` creates one;
linking the main checkout's is enough).

| Stage | Hooks (`.pre-commit-config.yaml`) |
| --- | --- |
| `pre-commit` | ruff and ruff-format, mypy, detect-secrets (`scripts/check_secrets.py`), the unit tests, plus whitespace, end-of-file, YAML/JSON syntax, large-file and merge-conflict checks |
| `pre-push` | the full test suite in four workers (allow a minute or so) and the web lint |

Remove worktrees with `python scripts/dev_checks.py remove-worktree <path>`,
never `git worktree remove` (see [AGENTS.md](AGENTS.md)).

## Before opening a PR

Run the gate by hand (native Windows: `python scripts/dev_checks.py gate`):

```bash
make lint && make test && make web-lint && make web-test
```

- Branch off `origin/main`, never your local `main`.
- The PR title needs a [Conventional Commits](https://www.conventionalcommits.org/)
  prefix (`feat:`, `fix:`, `chore:`, `docs:`, ...). PRs are squash-merged and
  Release Please parses the PR title, so an unprefixed title drops out of the
  changelog.
- CI must pass: the required checks and coverage floors (85% Python, 85%
  frontend) are listed in [docs/ci-cd.md](docs/ci-cd.md).
- Schema changes are forward-safe (no Alembic): new columns go into the
  idempotent `ALTER TABLE` list in `app/core/db.py`.
- Describe why the change exists, not just what changed.

Review, feedback handling and post-merge cleanup are described in
[AGENTS.md](AGENTS.md); follow them rather than improvising.

## External CI action pins

`scripts/action_pins.json` records the revision of every external CI action
(except `s3ntin3l8/.github` callers on `@main`). A well-formed but wrong pin
fails the job at action resolution, before any step runs, so pins are checked
against this record.

Bumping an action means editing the workflow and this record in the same
change, then running `make secrets-baseline` (commit SHAs read as high-entropy
strings). Dependabot's `github-actions` group cannot update the record, so its
PR stays red until a human reviews the new revision, copies it in and
regenerates the baseline; an action bump is a supply-chain decision.

Verify a SHA before recording it: `gh api repos/<owner>/<repo>/commits/<sha>`
answers 422 for a well-formed SHA that does not exist. The record vouches for
revisions it already carries, so a typo in a *new* action would otherwise pass.

## Project docs

- Architecture and data model: [docs/architecture.md](docs/architecture.md)
- Collection/merge logic: [docs/collection_logic.md](docs/collection_logic.md)
- CI/CD and releases: [docs/ci-cd.md](docs/ci-cd.md)
- Testing guide: [tests/README.md](tests/README.md)
- Security model: [docs/SECURITY.md](docs/SECURITY.md)

Remote development: Vite binds `127.0.0.1`; set `VITE_HOST` when remote access
is needed, and configure the API in network mode with an admin key, explicit
CORS origins and TLS (see [docs/SECURITY.md](docs/SECURITY.md)). A remote
browser cannot use the API's localhost administrator bypass through Vite.

<!-- mullion:pointer:start -->
See `AGENTS.md`'s Workflow Conventions section for our process rules.
<!-- mullion:pointer:end -->
