# CI/CD and releases

How Runway is built, gated and released. For the local equivalents of the
gates, see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Workflows

Workflows live in `.github/workflows/`. "Required" means a required status
check on `main`; everything else is advisory.

| Workflow | Trigger | What it does | Required |
| --- | --- | --- | --- |
| `ci-cd.yml` (CI/CD) | push and PR to `main` | See [CI/CD jobs](#cicd-jobs). | `test-python / lint`, `test-python / type-and-test`, `test-frontend / lint-and-test` |
| `codeql.yml` | push and PR to `main`, weekly | CodeQL static analysis. | `CodeQL` |
| `dependency-review.yml` | PR to `main` | Blocks PRs that add vulnerable or disallowed dependencies. PR-only: it does not re-run on `main`. | `dependency-review / dependency-review` |
| `security-validation.yml` | PR to `main`, manual | Trust-boundary and recovery tests: browser boundaries, reverse-proxy boundaries, native Windows swap/rollback. See [`security-validation.md`](security-validation.md). | no |
| `ux-validation.yml` | PR touching `webapp/`, `scripts/demo/`, `requirements-test.txt`; manual | Browser-level UX guard (overflow, tap targets, focus, fixed chrome) against the demo instance. | no |
| `visual.yml` | PR touching app, webapp, demo, `visual/`, requirements, Makefile | Before/after screenshots of `visual/scenes.json` scenes as a sticky PR comment, plus a separate `visual / health` check (new console errors, serious axe nodes). Runs `make visual-check`. | no |
| `visual-baselines.yml` | `/update-baselines` PR comment, manual | Regenerates the committed `visual/baselines/` shots on the PR (`make visual-baseline`). | no |
| `visual-refresh.yml` | weekly, manual | Refreshes README/PWA screenshots and the baselines; opens one rolling PR for each only when an image changed beyond tolerance. | no |
| `image-security.yml` | PR touching the image inputs, daily, manual | Builds the amd64 candidate image and scans it against the image policy; the daily `published` job audits the published `:edge` image and newest release; the `exception-window` job warns before the exception document expires. See [`security/image-policy.md`](security/image-policy.md). | no |
| `image-security-arm64.yml` | PR touching the image inputs, manual | The same candidate scan as a native build on an ARM runner. | no |
| `release-image-audit.yml` | manual (`image` = `ghcr.io/…@sha256:<digest>`) | Inventory and audit of one immutable released image. | no |
| `windows-native-suite.yml` | push to `main`, nightly, manual | The whole pytest suite on a native Windows runner in four shards. `scripts/pytest_shard.py` keeps whole modules together and balances by per-platform durations in `tests/shard_durations.{linux,windows}.json`; `make shard-weights` re-measures Linux, the manual `record-weights` input records Windows. | no |
| `frozen-verifier.yml` | push to `main`, nightly, manual | Builds the sidecar with PyInstaller on each platform and verifies the frozen binary against a signed fixture. Dispatch with `release-tag` to check a released version. | no |
| `keychain-synthetic.yml` | manual | macOS Keychain investigation harness: runs one synthetic case (`case` input) against a throwaway keychain. Evidence lives under [`evidence/`](evidence/README.md). | no |
| `release-please.yml` | push to `main`, manual (`main` only) | Opens/updates the release PR; on release, publishes the image and builds the sidecar. See [Releases](#releases). | no |
| `sidecar-build.yml` | reusable (`workflow_call`) | The sidecar build matrix: macOS `.dmg`, Windows NSIS `-setup.exe`, `.zip`/`.tar.gz` self-update payloads for all four targets, then an `attest` job writing `SHA256SUMS.txt` and Sigstore keyless `.sigstore.json` bundles. Asset names come from `scripts/sidecar_pkg/asset_names.py`; `tests/unit/test_sidecar_release_contract.py` pins the contract. See [`sidecar-build.md`](sidecar-build.md). | no |
| `sidecar-release.yml` | manual (`tag` input) | Wrapper around `sidecar-build.yml`; an empty `tag` builds artifacts only (installer testing from a branch), a tag uploads to that release. | no |
| `sidecar-edge.yml` | push to `main` touching sidecar, installer or the bundled `app/` modules | Builds with `label: edge` (version `<base>+edge.<sha>`) and overwrites the `edge` prerelease. `allow-partial` lets one flaky platform leg fail without blocking the rest. `tests/unit/test_sidecar_edge_app_paths.py` fails when the `paths:` filter stops covering the `app/` import closure the bundle carries. | no |
| `npm-audit-weekly.yml` | Mondays, manual | Audits the whole `webapp/` tree at every severity; the list goes to the job summary and one tracking issue labelled `npm-audit`. | no |
| `hermes.yml` | PR `opened` / `ready_for_review`, `@s3ntin3l8-hermes Review` comment | Automated PR review, once per PR. No `synchronize` trigger, so pushes never start a review loop. Skips `mullion/task-*` and release-please PRs. | no |
| `claude.yml` | `@claude` mentions on issues and PRs | Routes to the shared Claude Code workflow. | no |
| `cleanup-ghcr.yml` | weekly, manual (`dry-run`) | Prunes old GHCR image versions. | no |

Dependabot updates GitHub Actions, pip, npm and Docker weekly.
`.secrets.baseline` is tracked in git because CI's detect-secrets gate needs it.

### CI/CD jobs

- **`test-python`**: the shared `s3ntin3l8/.github` `ci-python.yml`. `lint` runs
  ruff check and format; `type-and-test` runs mypy, detect-secrets against
  `.secrets.baseline` (changed files only), pip-audit and pytest with Codecov,
  in four workers with `--dist loadfile`. `strict-checks: true` makes mypy and
  pip-audit hard gates. Coverage floor 85%.
- **`test-frontend`**: the shared `ci-node.yml` against `webapp/`: typecheck,
  Vite build, vitest coverage (floor 85%).
- **`windows`**: ruff, mypy and pip-audit on a native Windows runner.
- **`windows/specific`**: the `windows`-marked tests (native socket and handle
  semantics, installer packaging, console code pages), run by
  `scripts/dev_checks.py test-windows`.
- **`dependency-audit`**: `pip-audit`, plus `npm audit` of the shipped frontend
  dependencies at any severity and of the whole tree at moderate and up. The
  moderate cut-off is deliberate: it gates the `:edge` image, and a
  low-severity finding with no patched release in a dev-only package would
  otherwise keep the image red. `npm-audit-weekly.yml` keeps those findings
  visible.
- **`installer-check`**: compiles the NSIS installer with a placeholder exe, so
  a broken `.nsi` or missing installer asset fails on the PR.
- **`image-changed` and `build-docker`** (push only): `image-changed` compares
  the push against the Dockerfile's `COPY` sources and skips the rebuild when
  the image's contents cannot have changed. `build-docker` needs `test-python`,
  `test-frontend`, `dependency-audit` and `image-changed`, then calls the
  shared `docker-publish.yml` with `push-edge: true` for the `:edge` tag. The
  workflow passes the checked-out commit as the `COMMIT_SHA` build arg; the
  Dockerfile bakes it into the **final** stage as `RUNWAY_BUILD_SHA`, which
  Settings -> About shows. A skipped rebuild leaves `:edge` on the previous
  build and its SHA. The candidate runs `python -m scripts.audit_release_image`
  through `audit-command`; then that digest is signed and promoted, and the
  evidence is kept as the `release-image-audit-<image-slug>` artifact.

## Merge gate

Required checks on `main`: `test-python / lint`, `test-python / type-and-test`,
`test-frontend / lint-and-test`, `dependency-review / dependency-review` and
`CodeQL`. They are **not** required to be up to date with `main`
(`required_status_checks.strict = false`, deliberately): a PR that is merely
behind merges without a rebase, so a rebase never re-triggers the
pull-request workflows. Every conversation must be resolved. Squash-merge only;
the PR title becomes the commit message.

The merge queue is not an option: it needs an organization-owned repository
and the workflows would need a `merge_group` trigger.

What stands in for the up-to-date rule:

- `ci-cd.yml` and `codeql.yml` also run on the push to `main`, so a merge that
  breaks the integration is caught there (after the merge, not before).
  `dependency-review` is PR-only and has nothing to say about a merge that
  changes no dependency.
- The release PR is regenerated on every `main` push and requires the
  maintainer's approval, so no release is cut from a broken `main`.
- The `:edge` image only builds after the test and audit jobs pass on `main`.

### The `deepmerge` override

`webapp/package.json` overrides `deepmerge` to a patched fork because
[GHSA-gcqx-w48m-w2h4](https://github.com/advisories/GHSA-gcqx-w48m-w2h4) has no
patched upstream release. It is reached only through the dev-only
`vite-plugin-pwa` -> `workbox-build` -> `@rollup/plugin-node-resolve` chain;
the shipped tree is unaffected. The baseline the fork was checked against:

| | |
| --- | --- |
| Override | `deepmerge` -> `npm:@afeisite/deepmerge-clone@4.3.2` |
| Tarball | `https://registry.npmjs.org/@afeisite/deepmerge-clone/-/deepmerge-clone-4.3.2.tgz` |
| Integrity | `sha512-qu46OwxL9O2JrCoKOwdUVYX7GGaCDXLh1PPPmw0o4Zn+NqyJFEfR5xmonC92qVEOygBdEqzpY1ozGMiRAePcOQ==` |
| Diffed against | upstream `deepmerge` 4.3.1, all three shipped builds: `index.js`, `dist/cjs.js` and `dist/umd.js` |
| The diff | in each of the three, one `getKeys()` filter dropping `__proto__`, `constructor` and `prototype` (the key-name deny-list the advisory recommends) |
| Contents | eight files: `package.json`, `index.js`, `index.d.ts`, `dist/cjs.js`, `dist/umd.js`, `changelog.md`, `readme.md`, `license.txt`. No dependencies, no install script |
| Advisory | affects `>= 4.2.0, <= 4.3.1`, no patched version |
| Licence | MIT |
| Provenance | no SLSA attestation; single maintainer (`w530385371`); repository `MrMoveon/deepmerge-clone` |

The fork is trusted on the strength of that diff and the pinned integrity hash,
not the publisher's track record. A bump repeats the diff against the same
upstream baseline in all three builds and keeps the hash in step with
`webapp/package-lock.json`; `tests/unit/test_webapp_lockfile.py` asserts the two
agree.

## Releases

Releases are managed by **Release Please** (`release-please.yml`,
configured by `release-please-config.json`):

- Conventional Commits decide the bump: `feat:` minor, `fix:` patch,
  `feat!:` major; `chore:`/`docs:`/`test:` cut no release.
- On qualifying commits to `main`, Release Please opens a PR updating
  `CHANGELOG.md`, `package.json`, `pyproject.toml` and `app/__init__.py`.
  An unprefixed PR title silently drops out of the changelog.
- Merging that PR creates the GitHub Release and tag, which triggers the
  channel builds below.
- To force a version jump, tag manually, push the tag and create the GitHub
  Release by hand; Release Please continues from there.

| Channel | Image tags | Sidecar | Source |
| --- | --- | --- | --- |
| stable | `:latest` plus version tags | `vX.Y.Z` assets | a stable release |
| beta | `:beta` plus version tag | `vX.Y.Z-beta.N` assets | a prerelease (tag contains `-beta.`) |
| edge | `:edge` | the overwritten `edge` prerelease | every push to `main` that changes the image / sidecar inputs |

The release job runs `docker-publish.yml` with `push-release: true` (the
`release-channel` input selects stable or beta) and `sidecar-build.yml` for the
sidecar assets. The sidecar assets carry `SHA256SUMS.txt` and Sigstore keyless
bundles; the image is signed with cosign by the shared workflow. Verification
commands are in [`SECURITY.md`](SECURITY.md). While
`release-please-config.json` sets `prerelease: true`, every release is a beta;
promoting a stable release means removing prerelease mode first.

### The release PR's checks

Release Please pushes the release branch with the workflow's own
`GITHUB_TOKEN`, and GitHub holds `pull_request` runs from such a push. The PR
therefore sits blocked with its required checks pending until someone approves
the held runs; `allow_auto_merge` is off, so the merge is always manual.
Approving the runs re-checks the release commit with the six PR workflows
(`CI/CD`, `CodeQL`, `Security Validation`, `Image Security`,
`Image Security (arm64)`, `Dependency Review`). An administrator merge
(`enforce_admins` is off) skips them; `CI/CD` and `CodeQL` still run on `main`
afterwards, but the four PR-only workflows never see that commit.
