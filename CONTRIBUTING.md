# Contributing

## Setup

```bash
make install    # venv + Python deps + webapp deps + git hooks (pre-commit & pre-push)
```

On native Windows, install Python 3.12+ and Node.js 22.22.2+ (including npm 11.11+).
The supported Node versions are `^22.22.2 || ^24.15.0 || >=26.0.0` — npm **rejects**
Node 23.x and Node 24.0–24.14.
`winget install OpenJS.NodeJS.LTS` puts Node in the per-user PATH, and that
only applies to *new* processes — start a fresh shell (or a fresh agent
session) before `node`/`npm` resolve. Then run these commands in PowerShell
from the repository root; Make and WSL are not required:

```powershell
python scripts/dev_checks.py install
python scripts/dev_checks.py gate
```

Individual commands are `lint`, `mypy`, `unit`, `test`, `web-lint`, `web-test`,
`install-hooks`, `install-python`, `gate`, `test-shard`, `record-shard-weights`,
`test-windows` and `remove-worktree`; the
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
silently removes it from all 43 platform entries; npm 11.11 and later write
it. That is why the npm version is pinned rather than left to whatever happens
to be installed. `tests/unit/test_webapp_lockfile.py` fails if the field goes
missing.

The lockfile currently carries the field on **43** entries — 16 `@img/sharp-*`,
13 `@rollup/rollup-*`, 6 `@rolldown/binding-*`, 4 `@tailwindcss/oxide-*` and 4
`lightningcss-*`. The test pins **35** of them — the 16 sharp, 13 rollup and 6
rolldown packages — so a regeneration that drops the field from one of those
still fails. The eight newer oxide and lightningcss entries are not in the
fixture, so stripping the field from them alone would not.

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

One Windows-native trap catches every test that talks to a local HTTP server.
A server that rejects a request on its headers alone — the settings server's
`Origin` guard, its `Host` check, a size gate — answers and closes without
reading the request body. On Windows that close is a TCP reset rather than a
graceful close, and the reset discards the response the client has not read
yet, so the client sees `ConnectionAbortedError` (WinError 10053) instead of
the status code it is asserting on. Linux and macOS send a FIN there and the
response arrives normally, so the failure only ever surfaces on the Windows
runner. A request the server rejects on its headers must therefore carry no
payload: send `Content-Length: 0`, or declare the length and send nothing.
`tests/unit/test_sidecar_pairing.py` and
`tests/unit/test_keychain_sources_settings.py` both work that way.

The other Windows-native trap is the machine's port space rather than the
suite's. Windows picks the source port when an unbound socket connects, and it
can pick one that is already bound — `WinError 10048` (WSAEADDRINUSE) — when
the dynamic range is crowded. An IP Helper `portproxy` rule that forwards a
port to itself is a way to crowd it: with `0.0.0.0:8765 → 127.0.0.1:8765` on
the machine and the local Runway server down, that rule is the only listener
on 8765, so every connection to it is forwarded back to itself and IP Helper
multiplies sockets until the range is gone. The suite then fails in
`tests/windows_socketpair.py`. Run `netsh interface portproxy show v4tov4` and
delete any rule whose listen address covers its connect address;
`netsh int ipv4 show dynamicport tcp` shows how wide the range is. The
socketpair helper retries a transient port error on a fresh socket, so a
crowded machine no longer fails the run — but the machine still needs the fix.

Git hooks live in the repository's shared `.git/hooks`, so every worktree of
this repository writes into the same two hook files. Install them once with
`make install-hooks`: the hook this repository installs resolves its
interpreter when it runs, so it keeps working no matter which worktree
installed it and which worktrees have since been removed. A worktree that
commits still needs its own `.venv`, because the mypy and pytest hooks
resolve the interpreter from the checkout being committed. `make install`
provides one; linking the main checkout's `.venv` into the worktree is
enough, because the runner takes the repository root from its own
path and runs the tests with that root as the working directory, so the
checkout's own sources are the ones under test. The pre-push hook runs the
full suite in four workers, so allow it a minute or so.

The hook script is tracked in the repository (`scripts/hooks/git-hook.sh`) and
**copied** into `.git/hooks`, so the installed copy goes stale when the
tracked one changes: re-run `make install-hooks` after editing it.

**Remove a worktree with `python scripts/dev_checks.py remove-worktree <path>`,
not with `git worktree remove`.** On Windows a worktree that links the main
checkout's `.venv` into itself holds a directory junction, and
`git worktree remove` follows it: it deletes the *target's* contents, so the
main checkout loses its virtualenv and every other worktree that shared it
loses it too. (On POSIX git unlinks the symlink and leaves the target alone,
so this is a Windows hazard — the helper is the safe way to remove a
worktree everywhere.) The helper drops the links first — only the links —
and then hands the worktree to git. It needs no virtualenv of its own, so it
still works when the environment is what is broken. It also checks the target
before it unlinks anything: a path that is not a worktree of this repository,
the main checkout itself, or a worktree with uncommitted work is refused with
its links left in place.

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
