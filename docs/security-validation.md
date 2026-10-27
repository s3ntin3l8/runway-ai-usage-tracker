# Security audit validation

This remediation continues [#600](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/600) after merged PR #601. Validation uses synthetic secrets, temporary databases/config directories, and isolated services.

This document has two parts. **How the security gates work** is the durable part —
it describes the checks that run in CI and how to run them locally. **Results**
records what a specific run found, and is dated.

## How the security gates work

### Durable signature receipts

Concurrent claims, new database connections, rotation, expiry, storage failure,
ingestion idempotency and credential-manifest replay. Invalid requests also
consume valid signatures; retries sign fresh timestamps.

### Update verification

A real published repository signature and public transparency evidence, tampered
payloads, wrong identity/issuer, bounded bundles and unavailable trust refresh.
The updater verifies before extraction or installation. Frozen Linux CLI
verification accepts the real fixture and rejects tampering; the
[Frozen Verifier workflow](../.github/workflows/frozen-verifier.yml) repeats it
for Linux tray, Windows and macOS, on pushes to `main` and nightly.

### Browser matrix

15 passing checks across Chromium, Firefox and WebKit for hostile-origin writes,
Host rejection, omitted sidecar fleet keys, authenticated network access and
remote Vite authentication. Run it locally with:

```bash
npm --prefix webapp run test:security
```

In CI the matrix runs in Microsoft's Playwright image, pinned to the digest
`sha256:47b510dd9230bf0b9bfc9b20102241e577ea7a99334ab4c3ca19f19770e9b8ea` of
`mcr.microsoft.com/playwright:v1.64.1-noble`: the browsers and the system
libraries they need are baked into the image, so the job runs no apt
transaction and downloads no browser. The tag follows the locked
`playwright-core` version in `webapp/package-lock.json`, and
`python scripts/check_workflow_pins.py` fails the build when the two disagree.

The reason is [#876](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/876):
`playwright install --with-deps` always runs `apt-get update` against the
runner's apt mirror, and a stalled mirror spent 39 minutes of the job's budget
before the browser checks ran at all. Resolve the digest again after a
Playwright bump with:

```bash
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://mcr.microsoft.com/v2/playwright/manifests/v1.64.1-noble -D - -o /dev/null | grep -i docker-content-digest
```

Two things about that image are worth knowing before the job goes red again. It
carries **no Python** — the `ubuntu:noble` base has none — so the job provisions
one with `setup-python`, whose Linux build has an **empty RUNPATH** and finds
`libpython3.12.so.1.0` only through `LD_LIBRARY_PATH`; the job therefore
registers `${pythonLocation}/lib` with the dynamic linker, so the interpreter
starts wherever the browser matrix starts it. And the steps of a container job
run as **root**, which Firefox refuses to do with a home owned by somebody else,
so the steps that start the browsers point `HOME` at root's home.

### UX suite

The browser-level guard for what jsdom cannot see. The axe guard and the
contrast check run in jsdom, so neither can catch a page that scrolls sideways
on a phone, a control that is present but too small to hit, a focus ring
painted outside the viewport, or the fixed bottom nav sitting on top of the
content it should sit above.

`npm --prefix webapp run test:ux` runs Chromium against the demo instance —
the security suite's own server boots an empty database, and a layout
assertion against an empty app proves nothing: the column that overflows only
overflows once there are rows in it. Each route waits for the seeded data to
be on screen before it is measured, and a rule that matched nothing at all
fails rather than passes quietly.

It runs in CI on every pull request that touches the webapp, the demo's own
scripts, the test dependencies or the workflow itself, in the same pinned
Playwright image as the browser matrix and with the same linker registration.
Chromium only: this suite is about layout, and the browser matrix already
covers the three browsers.

### npm cache

The browser matrix installs the webapp dependency tree with
`npm --prefix webapp ci` on every pull request, so the job caches it through
`setup-node`'s own `cache: npm` option, keyed on the hash of
`webapp/package-lock.json` — the same option the `dependency-audit` job uses.
The dependency tree is therefore downloaded once per lockfile rather than on
every pull request. It needs no `actions/cache` reference of its own: the cache
action runs inside `setup-node`, so `scripts/action_pins.json` records no action
the workflows never name. `npm ci` still installs from the lockfile — the cache
restores the *download* cache only, and `npm ci` deletes `node_modules` first —
so a stale cache cannot change the installed tree.

### Actual nginx

Authenticated `auth_request`, stripped identity headers, forged direct headers
from an untrusted loopback address, preserved immediate peer and independently
signed sidecar ingestion. Run it with an isolated `RUNWAY_CONFIG_DIR` and
Docker:

```bash
RUNWAY_PROXY_TEST=1 pytest tests/security/test_real_proxy.py
```

### CI references

External actions and reusable workflows are pinned to commit SHAs;
`python scripts/check_workflow_pins.py` enforces this in CI.

The one exception is the owner's own `s3ntin3l8/.github` repository (the shared
reusable workflows and composite actions). Its callers track `@main`, like every
other repository of the owner, and the check accepts that ref and no other there
(a branch, a tag or a short SHA still fails). Everything it holds has one owner
and is gated by that repository's required checks, so a SHA pin adds no trust
boundary, only a bump of the callers, the record and the baseline after each fix.
References on `@main` are not recorded in `scripts/action_pins.json`. Third-party
actions and images stay pinned.

The recorded revision of each action lives in `scripts/action_pins.json`, and
the check runs in **both directions**: a reference the record does not cover is
reported, and a recorded revision no workflow uses is reported too, so the
record cannot drift away from the workflows. A well-formed SHA that does not
exist upstream fails the job at action resolution before any step in it runs —
that is how the image-policy tripwire stayed dead for a week — so the pin is
compared against the record, not only checked for shape.

The record vouches for the revisions it already carries, so a revision is
verified upstream before it is recorded: `gh api repos/<owner>/<repo>/commits/<sha>`
answers 422 for a well-formed SHA that does not exist. Bumping an action means
editing the workflow and the record in the same change and re-running
`make secrets-baseline`; Dependabot's `github-actions` group cannot update the
record, so its pull request stays red until a human reviews the new revision.

### Release image audit

The Release Image Audit dispatch workflow accepts an immutable Runway image index
digest, pulls each architecture through Docker authentication, then passes
read-only archives/SBOMs to digest-pinned Syft and Grype containers. It retains
CycloneDX SBOMs, package inventories, vulnerability results and scan evidence.
The release policy blocks untriaged Critical/High findings, scanner failures,
stale databases and incomplete results, with artifacts retained.

See [image policy](security/image-policy.md) and
[remediation evidence](security/image-remediation.md); raw lower-severity and
excepted findings remain visible.

### Update archive hashing

Update archive hashing streams 1 MiB chunks, and verification rejects signature
bundles above 1 MiB. The updater bounds actual response bytes before
verification: archives 256 MiB, bundles 1 MiB, checksums 4 KiB and metadata
4 MiB. Oversized local archives fail before hashing. Overflow stops retries,
closes responses and preserves installed/rollback copies; release packaging
checks the same payload limits.
[#605](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/605) records
this hardening.

## Results

### Release image scan (2026-10)

Fresh Python server/desktop/sidecar tooling and full npm dependency audits
reported no known vulnerabilities at validation time. This is separate from the
released image inventory.

The scanned image index was
`sha256:fd1e442ff3cb8e505ee0a35bf409c4b4d2aac1092ef5cc0efaed5f47b0e46f40`:
amd64 `sha256:bf3d49e01ee7a99c1364225a1e19ab444ba2b621084ac43cc9443b0c0860cc3f`,
arm64 `sha256:2252962d646fd78e9736266de102c8de4d01b1f6e1c39d7bcda2a3e124ea80fc`.
Each reports 477 vulnerability matches (28 Critical, 147 High, 156 Medium, 28
Low, 100 Negligible, 18 Unknown); 144 have reported fixes. Matches are 429
Debian, 36 binary and 12 Python (pip), not proven exploitability.
[#602](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/602) tracks
triage, base/OS/tooling refresh and rescanning a replacement release.

### Authentik/Traefik TLS matrix (2026-10-12)

The actual Authentik/Traefik TLS matrix passed on 2026-10-12 using the existing
real core/outpost/proxy and an isolated synthetic Runway application. See the
[execution record](security/evidence/603-authentik-traefik-2026-10-12/README.md)
for versions, sanitized configuration, outcomes, immediate-peer logs and
teardown.

Physical browser DNS rebinding/Local Network Access behavior and native
installer/update/restart/rollback QA remain tracked in
[#603](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/603). This
Linux SSO browser run, synthetic nginx identity and frozen-verifier CI do not
substitute for those remaining checks. The
[physical acceptance runbook](security/physical-validation.md) covers the full
matrix; #603 remains open.

## Related

- [`SECURITY.md`](SECURITY.md) — the trust policy.
- [`security/README.md`](security/README.md) — index of the security tree.
- [`ci-cd.md`](ci-cd.md) — the workflows these gates live in.
