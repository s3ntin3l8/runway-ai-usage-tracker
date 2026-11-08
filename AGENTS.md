# AGENTS.md — Runway (AI Usage Tracker)

This file is the single source of truth for this repo's workflow conventions and
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

<!-- mullion:briefing:start -->
Runway's cross-component correctness contracts live in `.claude/skills/runway/SKILL.md`; consult it when implementing, debugging, refactoring, testing, or reviewing changes to collection, credentials, ingestion, accounting, and shared schemas. The reviewer role at `.claude/agents/runway-reviewer.md` reads that skill first and applies the same contracts when review is delegated.

## Workflow Conventions

Never commit directly to the default branch. Always branch and open a PR.

Branch off the latest remote default branch, never off your local one: `git fetch origin && git checkout -b <branch> origin/<default>`. A local default branch is routinely stale, which is what makes a PR show up as "out-of-date with the base branch" the moment it's opened.

Work in a dedicated worktree per branch, under `.wt/<branch>`, off the latest remote default branch: `git fetch origin && git worktree add .wt/<branch> -b <branch> origin/<default>`. A fresh worktree does not inherit installed dependencies — reinstall them there before testing or building.

Commit and PR titles need a Conventional Commits prefix (`feat:`, `fix:`, `chore:`, ...).

Squash-merge PRs — the PR title becomes the commit message on the default branch.

Require green CI before merging.

Run a self-review pass on your own diff before declaring done.

Request an automated/bot review through the review relay (`mullion review`, or the `request_review` tool) — once after opening the pull request, and again per head commit after pushing fixes for a changes-requested review, then wait for it. A review comment that mentions the review bot is for humans, not agents; if no relay is set up, tell the human instead. One review per pull request at a time: a review that is already running is not re-requested — the request is refused with its run id, and that refusal is the answer, not an error to retry: wait for it, or stop it with stop_review. A review takes 10-30 minutes; check review_status once instead of asking again. After pushing fixes for a changes-requested review, ask again only when no review is in flight. Once the review bot has approved, apply its remaining suggestions and push without requesting another review, even though the head moved: small targeted fixes do not change the verdict, and re-reviewing them only breeds more nits. Never re-trigger a review with an @mention of the review bot: that path is for humans, and it queues another round behind the running one.

Fixing the code is not enough to address review feedback — reply to each inline comment via the GitHub API, then resolve the thread via the GraphQL `resolveReviewThread` mutation, rather than pushing a silent fix. Reply with `gh api repos/{owner}/{repo}/pulls/<pr>/comments/<comment_id>/replies -f body="…"`; resolve with `gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<thread_id>"}) { thread { isResolved } } }'`, taking the thread id from a `reviewThreads` query on the pull request, not from the REST comment id.

File a tracked issue for anything a plan defers, blocks, or descopes, and link it from the PR (using the repository's issue template if it has one) — a footnote in a plan doc is not a durable record. Write each one as a spec an agent can implement on its own (scope, acceptance criteria), not as a note. Title it the way this repo titles commits and PRs, reusing its existing scope names rather than coining new ones; attach it as a sub-issue to the matching open epic when one exists; and set GitHub's `blocked_by` dependency where one issue cannot start before another closes.

After a merge, delete the local and remote branch and remove any associated worktree.

Before pushing, run the full lint/typecheck/test/format gate.
<!-- mullion:briefing:end -->

## Repo specifics

- `main` does *not* require branches to be up to date
  (`required_status_checks.strict = false`, deliberately — see
  [`docs/ci-cd.md`](docs/ci-cd.md)), so a PR that is merely behind still merges
  without a rebase.
- Hermes reviews a PR automatically when it opens non-draft (a draft is reviewed
  on `ready_for_review`), except `mullion/task-*` branches (Task Master's own
  reviewer is the terminal review there), fork PRs, and PRs authored by
  `s3ntin3l8-hermes[bot]` or `github-actions[bot]` (Dependabot PRs are reviewed
  deliberately). In a Mullion session ask for another round through the relay;
  it refuses a review that is already running or already landed for this head
  SHA, and accepts a new commit. Outside a Mullion session, comment
  `@s3ntin3l8-hermes Review`.
- Release Please parses the squash-merged PR title: an unprefixed title
  silently drops out of the changelog.
- The full gate is `make lint && make test && make web-lint && make web-test`
  (native Windows: `python scripts/dev_checks.py gate`). `make install` wires
  the same checks into git hooks — pre-commit runs ruff, mypy, detect-secrets
  and the unit tests, pre-push the full suite. Coverage floors: 85% Python,
  85% frontend.
- A fresh clone or worktree inherits nothing: run `make install` first (native
  Windows: `python scripts/dev_checks.py install`).
  The frontend half of the gate needs Node 22.22.2+ and npm 11.11+
  (`webapp/package.json` pins both with `devEngines`).
- The branch requires every conversation resolved, and an `APPROVED` review can
  still carry an open inline thread. The REST `PUT /pulls/<n>/merge` endpoint
  silently skips that gate, so don't merge through it.
- Worktrees live at `.wt/<slug>`, a flat slug rather than the branch name (`.wt`
  is a container `app/services/project_label.py` knows about, and a branch such
  as `feat/foo` would nest a directory).
- Remove a worktree with `python scripts/dev_checks.py remove-worktree <path>`,
  never `git worktree remove`: a worktree that links the main checkout's
  `.venv` holds a directory junction, and `git worktree remove` follows it and
  deletes the main checkout's virtualenv.

Hook interpreter details, the frontend toolchain pins and the CI action-pin
procedure (`scripts/action_pins.json`) are in [`CONTRIBUTING.md`](CONTRIBUTING.md)
— read it before bumping a CI action or changing the hook script.

## Everyday commands

`make help` lists them all. The ones with a catch:

- `make dev` alone no longer sees your local CLI logins (the server reads no
  credential files); `make dev-all` (server + Vite on :5173 + sidecar) collects
  from them.
- A single test file: `pytest tests/path/to/test_file.py`.
  `make test-shard SHARD=1 SHARDS=4` is the split native Windows CI uses;
  `make shard-weights` re-measures the per-module durations that balance it.
- `make sidecar-registry` regenerates the sidecar's baked credential rules after
  editing `app/core/registry.json` — the baked block is generated, never
  hand-edited.
- `make secrets-baseline` regenerates the detect-secrets baseline after vetting
  a new detection. A known secret is matched by filename, hash and type, but the
  baseline stores its line number: editing a baselined file moves it, so re-run
  in the same change or the CI gate fails (and the baseline conflicts on the next
  rebase).
- `make screenshots-audit AUDIT=<slug>` writes desktop, tablet and phone captures
  into `docs/audit/assets/<slug>/`, which is **local only** (gitignored); the audit
  *report* is the committed artifact, named `YYYY-MM-<scope>-<topic>.md` — read
  [`docs/audit/README.md`](docs/audit/README.md) before writing one.
- **Look at every UI change before calling it done.** Run
  `make visual-check ROUTES=<the pages you touched>` (Docker; routes are scene names or
  paths from `visual/scenes.json`, e.g. `ROUTES=history,/provider`): it renders the merge
  base and your working tree against `make demo` in the pinned
  [visual-kit](https://github.com/s3ntin3l8/visual-kit) image and writes
  `visual/.out/check/report/` (`index.html` side by side, `summary.md`). Open the changed
  and new shots and read the deltas (axe, console errors, horizontal overflow, unnamed
  controls): a diff is intended, a regression, or flake (then fix the demo or the settle
  logic in `visual/hooks.mjs`, or give that one scene a `maxDiffPixels` with the reason;
  never raise the threshold for every scene). The PR description gets a **Visual check**
  section: routes, what changed and why, axe delta. A new page or state needs a scene in
  `visual/scenes.json` (tags `check`, `audit`, `presentation`). After changing the demo or a
  scene, run `make visual-check REPEAT=2` (the launcher recaptures the working tree
  itself): it must report no determinism failure. `baseHarness` renders the base with the
  head's `scripts/demo`, seed (`seed_demo.py`) included, and the merge base's app, `demo`
  target, `requirements*.txt` and `webapp/package-lock.json`. So a seed edit moves both
  sides and the check against the merge base shows no diff for it (the baseline scenes
  are still compared with their committed baselines, which reports the change;
  `make visual-baseline` regenerates them), and a PR that changes `scripts/demo`
  together with the app code it depends on can fail to start its base demo, and then every scene reports "new" (the
  reason is only a stderr line). The same check runs on
  UI PRs in CI (advisory). Scenes flagged `"baseline": true` also have committed shots in
  `visual/baselines/`: regenerate them in the PR that moves those pages
  (`make visual-baseline`, or comment `/update-baselines` on the PR). The README, PWA and
  landing screenshots come from `make screenshots` / `make screenshots-landing` and are
  refreshed weekly by `.github/workflows/visual-refresh.yml` (one rolling PR each for the
  screenshots and the baselines); regenerate them yourself when a screen they show changes
  (the loop re-encodes non-deterministically: do not commit `docs/screenshots/*.webm|mp4|jpg`,
  `make screenshots` removes them). The demo's clock is `DEMO_NOW` (see `visual/scenes.json`);
  spec and profiles: visual-kit's `CONTRACT.md`; the image is pinned in `visual/Dockerfile`.

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
- **The server reads no credential files except its own `github_oauth.json`;
  credentials arrive via sidecar, env var or Settings.** File rules in
  `registry.json` are evaluated by the sidecar only (`_server_may_read` gates the
  server, before any `~` expansion), and collectors never touch the host home.
  `tests/unit/test_server_reads_no_credential_files.py` enforces it.
- **Dev and prod never share a data dir.** SQLite is single-writer; `make dev*`
  defaults `RUNWAY_CONFIG_DIR` to the gitignored `./data`.
- **Schema changes are forward-safe (no Alembic).** New columns are added by
  `init_db`'s idempotent `ALTER TABLE` list in `app/core/db.py`; an image
  bump must never break an existing DB.
- **Adding a card field** means updating `LimitCard` in
  `app/models/schemas.py`, the mirror in `webapp/src/api/types/usage.ts`, and the
  interface example in [`docs/api-reference.md`](docs/api-reference.md).
- **Collector code declares its taxonomy** — `data_source` (api / web / local)
  and `input_source` (config / server / sidecar) — and anything `local` runs
  in the sidecar, never the server. See
  [`docs/architecture.md`](docs/architecture.md#collectors) and
  [`docs/collection_logic.md`](docs/collection_logic.md).
- **Branding:** `assets/logo.svg` is canonical; every other surface (favicon,
  PWA/tray/installer art) is derived. Run `make logo` to regenerate; never
  hand-edit a derived asset. See [`docs/branding.md`](docs/branding.md).
- **Style:** Python is `async` with explicit type hints, and collectors degrade
  to an "Error Card" instead of crashing. The
  frontend (`webapp/`) uses semantic token utilities (`bg-surface-1`,
  `text-fg-muted`), never raw hex. Sidecars only extract/forward raw data; the
  server does the heavy lifting.
