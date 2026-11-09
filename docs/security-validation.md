# Security validation

How the security gates work and how to run them locally. The checks use
synthetic secrets, temporary databases and config directories, and isolated
services. The workflows are listed in [`ci-cd.md`](ci-cd.md); the trust policy
is [`SECURITY.md`](SECURITY.md).

| Gate | Workflow | Covers |
| --- | --- | --- |
| Static analysis | `codeql.yml` | CodeQL on push, PR and weekly. Required. |
| Dependency review | `dependency-review.yml` | PRs that add vulnerable dependencies. Required. |
| Python and npm audits | `ci-cd.yml` | `pip-audit` and `npm audit` (see [`ci-cd.md`](ci-cd.md)). |
| Trust boundaries | `security-validation.yml` | Browser matrix, real reverse proxy, native Windows recovery (below). |
| Browser UX | `ux-validation.yml` | Layout guard (below). |
| Image vulnerabilities | `image-security*.yml`, `release-image-audit.yml` | Syft/Grype scans against the image policy (below). |
| Packaging | `frozen-verifier.yml` | Frozen sidecar binaries verify a signed fixture. |

## Durable signature receipts

Tests cover concurrent claims, new database connections, rotation, expiry,
storage failure, ingestion idempotency and credential-manifest replay. Invalid
requests also consume valid signatures; retries sign fresh timestamps.

## Update verification

Tests cover a real published repository signature with public transparency
evidence, tampered payloads, wrong identity or issuer, bounded bundles and an
unavailable trust refresh. The updater verifies before extraction or
installation. Frozen Linux CLI verification accepts the real fixture and rejects
tampering; the [Frozen Verifier workflow](../.github/workflows/frozen-verifier.yml)
repeats it for Linux tray, Windows and macOS on pushes to `main` and nightly.

Update archive hashing streams 1 MiB chunks. The updater bounds actual response
bytes before verification (archives 256 MiB, bundles 1 MiB, checksums 4 KiB,
metadata 4 MiB; `scripts/sidecar_pkg/update_limits.py`). Oversized local
archives fail before hashing; overflow stops retries, closes responses and
preserves the installed and rollback copies. Release packaging checks the same
limits.

## Browser matrix

Chromium, Firefox and WebKit check hostile-origin writes, Host rejection,
omitted sidecar fleet keys, authenticated network access and remote Vite
authentication. Run it locally with:

```bash
npm --prefix webapp run test:security
```

The `browser-boundaries` job runs in Microsoft's Playwright image pinned by
digest, so it runs no apt transaction and downloads no browser. The image tag
follows the locked `playwright-core` version in `webapp/package-lock.json`, and
`python scripts/check_workflow_pins.py` fails when the two disagree. After a
Playwright bump, resolve the new digest with:

```bash
curl -s -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://mcr.microsoft.com/v2/playwright/manifests/<tag> -D - -o /dev/null | grep -i docker-content-digest
```

The job caches the npm download cache through `setup-node`'s `cache: npm`
(keyed on `webapp/package-lock.json`); `npm ci` still installs from the
lockfile. The image's Python and Firefox-as-root quirks are handled by
comments in the workflow.

## UX suite

`npm --prefix webapp run test:ux` is the browser-level guard for what jsdom
cannot see: a page that scrolls sideways on a phone, a control too small to
hit, a focus ring painted outside the viewport, fixed chrome covering content.
It runs Chromium against the **demo instance** (see [`demo.md`](demo.md)), not
the empty-database server of the security suite, because layout assertions on
an empty app prove nothing. Each route waits for seeded data before it is
measured, and a rule that matched nothing fails rather than passing quietly.
`ux-validation.yml` runs it on PRs that touch the webapp, the demo scripts, the
test dependencies or the workflow, in the same pinned Playwright image.

## Real reverse proxy

The `proxy-trust-boundaries` job runs nginx in Docker: authenticated
`auth_request`, stripped identity headers, forged direct headers from an
untrusted loopback address, preserved immediate peer, and independently signed
sidecar ingestion. Locally, with an isolated `RUNWAY_CONFIG_DIR` and Docker:

```bash
RUNWAY_PROXY_TEST=1 pytest tests/security/test_real_proxy.py
```

The same job runs `check_workflow_pins.py` (below). The third job,
`native-windows-recovery`, runs the swap, rollback and recovery regressions in
`tests/unit/test_self_update.py` and `tests/unit/test_windows_native_boundaries.py`
on a native Windows runner.

## CI references

External actions and reusable workflows are pinned to commit SHAs;
`python scripts/check_workflow_pins.py` enforces this. The exception is the
owner's own `s3ntin3l8/.github` repository (shared reusable workflows), whose
callers track `@main` and are not recorded; the check accepts that ref and no
other there. Third-party actions and images stay pinned.

The recorded revision of each action lives in `scripts/action_pins.json` and the
check runs in both directions: a reference the record does not cover is
reported, and so is a recorded revision no workflow uses. A well-formed SHA that
does not exist upstream fails at action resolution before any step runs, so the
pin is compared against the record, not only checked for shape. The bump
procedure is in [`../CONTRIBUTING.md`](../CONTRIBUTING.md#external-ci-action-pins).

## Image scanning

`image-security.yml` (amd64) and `image-security-arm64.yml` build the candidate
image and scan its package inventory and SBOM with digest-pinned Syft and Grype
containers; a daily job audits the published `:edge` image and the newest
release. `release-image-audit.yml` takes an immutable image index digest, pulls
each architecture, and keeps CycloneDX SBOMs, package inventories and scan
results as artifacts. The policy blocks untriaged Critical/High findings,
scanner failures, stale databases and incomplete results; lower-severity and
excepted findings stay visible. See [`security/image-policy.md`](security/image-policy.md)
and [`security/image-remediation.md`](security/image-remediation.md).

## Related

- [`SECURITY.md`](SECURITY.md): the trust policy.
- [`security/README.md`](security/README.md): index of the security tree.
- [`evidence/README.md`](evidence/README.md): dated validation records.
- [`ci-cd.md`](ci-cd.md): the workflows these gates live in.
