# Published release security acceptance

Image promotion and sidecar signing are separate acceptance boundaries. An image
scan must describe the exact published digest. A frozen verifier must accept the
actual captured portable release bytes, not only a synthetic signed fixture.
Neither check replaces physical installation, restart, rollback, browser LNA or
real identity-provider validation tracked in #603.

## Portable release capture and frozen validation

Dispatch **Frozen Verifier** on the trusted source branch with `release-tag`
set to a versioned release such as `v3.0.0-beta.8`. (The workflow used to be
called Security Validation; the packaging half of it moved to its own
workflow when it stopped running on every pull request.) Rolling edge is deliberately
unsupported. The capture job requires Linux CLI, Linux desktop, Windows ZIP and
macOS ZIP, each with a checksum and modern Sigstore bundle. It records the release
ID, tag commit, asset IDs, sizes, GitHub digests when supplied, and locally computed
SHA-256 hashes. It checks checksums, runs the production updater verifier on all
four payloads, then rechecks tag and asset identity for replacement during capture.

The native frozen matrix uses that single captured asset set, rechecks its complete
inventory and hashes, and invokes the normal `--verify-update` diagnostic in each
frozen binary with an isolated config directory. A valid archive must pass;
tampered bytes, missing or malformed bundles and oversized archives must refuse
with the expected exit code and diagnostic. Nothing is installed or extracted.
Production Sigstore trust refresh requires network access; failures do not permit
a checksum fallback or a weakened signing identity.

`captured-release-updates` and `frozen-verifier-*` artifacts retain manifests and
results for 90 days. Before closing #606, link a successful run, record source
commit and capture manifest hashes, and confirm all four release-evidence reports
passed. Keep a durable local copy if evidence is needed beyond artifact retention.
For local investigation:

```bash
python -m scripts.capture_release_updates --release-tag v3.0.0-beta.8 --output /tmp/beta8-capture
python -m scripts.security_packaging_smoke --binary dist/runway-sidecar-cli \
  --release-dir /tmp/beta8-capture --platform Linux-CLI --report /tmp/beta8-linux-cli.json
```

Without release inputs, existing PR checks continue using the real signed fixture.
An explicit `--payload`/`--bundle` pair is also supported for packaging diagnostics.

## Published image closure

Record the published index and both architecture digests, raw SBOM/report artifacts,
database identity and scan time, exact reviewed exceptions, and successful signing
and promotion steps. Independently verify the public index signature against the
repository's Docker publishing workflow on main and GitHub's OIDC issuer. Confirm
the public versioned tag resolves to the audited index. Link the original baseline
and replacement evidence in #602 before closing it; source-only or candidate-only
validation does not establish replacement of released bytes.
