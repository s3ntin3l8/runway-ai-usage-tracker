# CI/CD and releases

How Runway is built, gated and released. This document was split out of
[`architecture.md`](architecture.md), where it was doing no service — a
workflow-by-workflow walkthrough is not an architecture.

For the local equivalents of the gates, see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Workflows

Workflows live in `.github/workflows/`.

- **`ci-cd.yml`** — on push/PR to `main`:
  - **`test-python`** — reusable `s3ntin3l8/.github` `ci-python.yml`:
    `lint` (ruff check + format) and `type-and-test` (mypy, detect-secrets
    against `.secrets.baseline`, pip-audit, pytest with Codecov). This repo
    passes `strict-checks: true`, so mypy and pip-audit are hard gates here
    rather than advisory findings, and `test-parallel: '4'` runs the suite in
    four workers (`--dist loadfile`, so every test module stays in one
    worker). `detect-secrets-scope: changed` scans only the files the push
    touched. Coverage floor 85% (the suite covers 91.6%).
  - **`test-frontend`** — reusable `ci-node.yml` against `webapp/` with
    strict checks: typecheck, Vite build, vitest coverage. Floor 85%. It
    passes `detect-secrets: false`: the Python job above already scans the
    repository, and doing it twice cost ~50s per run for the same answer.
  - **`windows`** — ruff, mypy and pip-audit on a native Windows runner.
  - **`windows/specific`** — the test modules whose subject is the Windows
    platform (native socket and handle semantics, installer packaging,
    console code pages), marked with the `windows` marker and selected by
    `scripts/dev_checks.py test-windows`.
  - **`installer-check`** — compiles the Windows NSIS installer with a
    placeholder exe on every PR, so a broken `.nsi` or missing installer
    asset fails here, not at release time.
  - **`image-changed` + **`build-docker`** — on push only: GHCR image via the
    shared `docker-publish.yml` with `push-edge: true` → the `:edge` tag.
    `image-changed` compares the push against the Dockerfile's `COPY` sources
    (the Dockerfile, `requirements.txt`, `app/`, `webapp/`) and skips the
    rebuild when the image's *contents* cannot have changed; if the base
    commit cannot be resolved it rebuilds.
    Because the Dockerfile bakes the commit in as `RUNWAY_BUILD_SHA`, a
    rebuilt image is never byte-identical to its predecessor, so a skipped
    rebuild leaves `:edge` serving the previous commit's image and that
    image's SHA. The SHA still names the exact build that is running — it is
    the SHA of the *build*, not of `main` — but it is no longer `main`'s tip.
    The shared workflow passes the checked-out commit as the `COMMIT_SHA`
    build arg and the Dockerfile bakes it in as `RUNWAY_BUILD_SHA`, so the
    running server reports the exact build it is in Settings → About — every
    `:edge` image ships the same version string, so the SHA is what tells two
    of them apart. The Dockerfile has to declare `ARG COMMIT_SHA` in its
    **final** stage (a builder-stage ARG never reaches the running image); the
    build fails loudly if the final stage uses it without the declaration.
    The workflow runs `python -m scripts.audit_release_image` on the candidate
    image through its `audit-command` input, then signs and promotes that
    digest; the evidence is kept as the `release-image-audit-<image-slug>`
    artifact.
  - Every job above except the two `test-*` ones and `image-changed` is
    advisory: none of them is a required status check.
  - Required checks on `main`: `test-python / lint`,
    `test-python / type-and-test`, `test-frontend / lint-and-test`,
    `dependency-review / dependency-review`, and CodeQL. They are **not**
    required to be up to date with `main` — see *The merge queue is not
    available here* below.
- **The merge queue is not available here.** The merge queue is documented as
    available on public repositories owned by *organizations* (and GitHub
    Enterprise Cloud), not on personal accounts, and this repository is owned
    by a personal account — GitHub's ruleset API rejects the `merge_queue`
    rule with *"Invalid rule 'merge_queue'"*.

    What does the job instead is `required_status_checks.strict = false`: a
    pull request no longer has to be up to date with `main` to merge, so it
    does not need a rebase — and a rebase is what used to re-trigger the
    pull-request workflows. Seven workflows here listen for `pull_request`
    (`ci-cd`, `codeql`, `dependency-review`, `hermes`, `image-security`,
    `image-security-arm64`, `security-validation`), and six of them re-run on
    a rebase; `hermes.yml` opts out with `types: [opened, ready_for_review]`.
    Measured: `fix/cookie-store-classification`, several commits behind
    `main`, reports `CLEAN` and merges without a rebase.

    The safety that the up-to-date rule was standing in for is covered
    elsewhere, in two layers. The required checks run again on the push to
    `main` — `ci-cd.yml` and `codeql.yml` both have a `push` trigger — so a
    merge that breaks the integration is caught on `main`: the difference
    from the up-to-date rule is *when* (after the merge, not before) and
    *who* (whoever picks up a red `main`, not the author whose pull request
    was bounced back). `dependency-review` is the exception: it is a
    pull-request-only check, so it does not re-run on `main` — it has nothing
    to say about a merge that changes no dependency. And the release pull
    request is regenerated on every `main` push, so it always builds current
    `main`: a broken `main` makes its checks red, and the maintainer's
    approval — which the release pull request deliberately requires — is the
    gate. No release is cut from a broken `main`. The `:edge` image is gated the same way:
    `build-docker` needs `test-python`, `test-frontend` and
    `dependency-audit` to pass on `main`. It audits the shipped (non-dev) frontend
    dependencies at any severity and the whole tree, build tooling included, at
    moderate and up: a low-severity finding with no patched release that is
    reachable only through a dev-only package would otherwise keep the job, and
    with it the edge image, red (#1033).

    Moving the repositories to an organization would enable the queue, but
    that is not only a settings change: the workflows that produce the
    required checks would need a `merge_group` trigger, and the advisory jobs
    would need to skip that event so the queue does not wait on the Windows
    runner. Neither is in place, because neither could be exercised here —
    an untested conditional path in CI is worse than no path at all.
- **`windows-native-suite.yml`** — the whole pytest suite on a native Windows
    runner, in four parallel shards, on `main` pushes and nightly. It used to
    run on every pull request, where it cost ~11 runner-minutes — the largest
    advisory cost in the repository — for coverage a Windows runner is not
    the only way to get. `scripts/pytest_shard.py` owns the split: it keeps
    whole test modules together (so the fixed-port helpers and
    `tests/conftest.py`'s import-time setup stay in one process) and balances
    them by measured runtime, recorded per platform in
    `tests/shard_durations.linux.json` and `tests/shard_durations.windows.json`
    -- a module that is slow on Linux is not necessarily slow on Windows. A
    shard runs the whole suite with the other shards' modules deselected by
    node id prefix, so pytest's own `collect_ignore` still decides what is
    collectible. Re-measure with `make shard-weights`; the Windows table is
    measured on a Windows runner by the workflow's manual `record-weights`
    job, because the durations are platform-specific. The frontend is not
    part of this job — `test-frontend / lint-and-test` already covers it on
    Linux.
- **`frozen-verifier.yml`** — the packaging smoke test: the sidecar is built
    with PyInstaller on each platform and the frozen binary is verified
    against a signed fixture. It used to run on every pull request, in four
    parallel jobs, which made it the largest advisory cost in the repository
    (7.6 runner-minutes per pull request, and the macOS leg is billed at ten
    times a Linux minute). On a pull request it verified the *standard signed
    fixture*; the release half of the job — the one that answers "does the
    packaged binary match the release" — only ever ran on a manual dispatch
    with a release tag, because that is what `capture-release` produces. It
    now runs on pushes to `main` and nightly, and the release path is
    unchanged: dispatch this workflow with a release tag. The nightly is the
    canary for toolchain drift — PyInstaller is pinned, so a new release of
    it only lands when the pin moves — but it can break the packaging when
    it does, and a push run cannot catch that, because it ran before the
    release did.
- **`security-validation.yml`** — the trust-boundary and recovery tests, in
    three jobs that share no state: the browser boundaries, which run in the
    digest-pinned Playwright image so the runner's apt mirror cannot spend
    the budget before the checks run ([#876](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/876));
    the real reverse-proxy boundaries, which need the Python set and Docker
    but no browser and carry the immutable-reference check; and the native
    Windows swap, rollback and recovery regressions. The packaging half of the
    old workflow lives in `frozen-verifier.yml`.
- **`image-security.yml` / `image-security-arm64.yml`** — the candidate
    image is built and scanned for known vulnerabilities. The amd64 leg runs
    on every pull request; the arm64 leg is a native build on an ARM runner
    and lives in its own workflow, filtered to the paths that can change the
    image. The daily `published` job audits the arm64 image that actually
    ships.
- **`release-please.yml`** — on push to `main` (or a manual `workflow_dispatch` run, `main` only, serialized): opens/merges the release PR
  from Conventional Commits (see *Releases*). When it cuts a release it also
  runs the shared `docker-publish.yml` with `push-release: true` (stable → `:latest` +
  version tags; beta → `:beta` + version tag, selected by `release-channel`)
  and calls `sidecar-build.yml` via `build-sidecar`/`publish-sidecar` —
  for stable and beta releases alike.
- **`sidecar-build.yml`** — reusable (`workflow_call`) sidecar matrix. It
  builds the release artifacts; the other two places that build with
  PyInstaller are `frozen-verifier.yml`, which smoke-tests the frozen binary,
  and the CI audit job, which installs it to audit it. Per release label
  (`vX.Y.Z`, `vX.Y.Z-beta.N`,
  or `edge`) it
  builds the macOS **`.dmg`** (ad-hoc signed `.app`, `create-dmg`) and
  Windows **NSIS `-setup.exe`** (`installer/windows/runway-sidecar.nsi`)
  installers, plus the `.zip`/`.tar.gz` self-update payloads for all four
  targets. Its `attest` job then writes `SHA256SUMS.txt` and Sigstore
  keyless `.sigstore.json` bundles. Every asset name comes from
  `scripts/sidecar_pkg/asset_names.py` (the updater's source of truth), and
  `tests/unit/test_sidecar_release_contract.py` pins it all together.
  `sidecar-release.yml` is a manual wrapper (`workflow_dispatch`; empty
  `tag` = build-only artifacts, handy for testing installers from a branch).
- **`sidecar-edge.yml`** — on push to `main` touching sidecar/installer
  code *or the shared `app/` modules the sidecar bundles*; calls
  `sidecar-build.yml` with `label: edge` (version stamped
  `<base>+edge.<sha>`) and publishes to the always-overwritten `edge`
  prerelease — the sidecar analog of the Docker `:edge` tag. A flaky
  non-Linux runner doesn't block the rest (`allow-partial`). The sidecar
  binary bundles `app/` code (`app.services.model_normalization` decides
  the cost bucket every collected event is billed under), so a change to
  those modules has to rebuild the sidecar too;
  `tests/unit/test_sidecar_edge_app_paths.py` computes the bundled `app/`
  import closure — from every spec's entry point and declared hiddenimports,
  following relative imports, and counting the package `__init__.py` files
  the bundle carries — and fails when the workflow's `paths:` filter stops
  covering it.
- **`hermes.yml`** — automated PR review. Auto-reviews exactly once per PR
  (`opened` when non-draft, or `ready_for_review`); on-demand re-reviews via
  a `@s3ntin3l8-hermes Review` comment. Deliberately no `synchronize`
  trigger — re-reviewing every push would be an unbounded review loop.
  Auto-review excludes `mullion/task-*` and release-please PRs; dependabot PRs
  are reviewed too, once the Dependabot secrets are mirrored.
- **`claude.yml`** — `@claude` mentions on issues/PRs route to the shared
  reusable Claude Code workflow.
- Also `codeql.yml`, `dependency-review.yml`, `cleanup-ghcr.yml`.
  Dependabot updates actions, pip, and npm weekly. `.secrets.baseline` is
  tracked in git — CI's detect-secrets gate needs it.

## Releases

Releases are managed by **Release Please**
(`.github/workflows/release-please.yml`):

- Uses Conventional Commits to determine version bumps: `feat:` → minor,
  `fix:` → patch, `feat!:` → major, `chore:`/`docs:`/`test:` → no release.
- On qualifying commits to `main`, Release Please opens a PR updating
  `CHANGELOG.md` and `package.json`.
- Merging that PR creates the GitHub Release and tag automatically — which is
  what triggers the release-channel image and sidecar builds above (stable →
  `:latest`, beta prerelease → `:beta`).
- To force a version jump (e.g. v1.0.0): tag manually, push the tag, create
  the GitHub Release by hand — Release Please picks up from there.

The release PR's own CI is held for approval, and the merge is always
somebody's. Release Please pushes the release branch with the workflow's own
`GITHUB_TOKEN`, and GitHub holds `pull_request` runs from a push like that,
so the release PR sits at `BLOCKED` with its required checks stuck at pending.
There is no auto-merge on this repository (`allow_auto_merge` is off).
Approving the held runs lets the required contexts report so the PR can be
merged normally; merging it with administrator privileges — `enforce_admins`
is off on `main` — skips the held runs entirely and leaves them unstarted.

Approval is not free, though. The release PR carries six workflows — `CI/CD`
and `CodeQL`, which also run on the push to `main`, and `Security Validation`,
`Image Security`, `Image Security (arm64)` and `Dependency Review`, which run
on a pull request but not on the push. What they re-check is the release
commit: a version bump and a changelog on top of commits that were each
tested on the pull request they came in through. One measured batch cost 20.8
runner-minutes and about an hour of wall clock — its last job waited 43
minutes for a hosted runner before starting. The administrator merge skips
those six runs and that hour; `CI/CD` and `CodeQL` still run on `main`
afterwards, as they do after any merge. What it gives up is the four
pull-request-only workflows' look at the release commit: they have no push
equivalent, so a release merged that way never reaches them.

The prerelease settings in `release-please-config.json` are temporary for
the 3.0.0 beta cycle (`prerelease: true`, `prerelease-type: beta.1`). Before
merging a stable release, follow
[issue #408](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/408)
to remove prerelease mode and promote 3.0.0; otherwise future Release Please
releases will also be beta versions.
