# Release image security policy

Runway blocks image promotion on every Critical/High Grype match unless an exact,
reviewed applicability exception exists. Whether the scanner reports a fix does
not change this rule. Medium, Low, Negligible and Unknown findings remain visible;
Python findings are separately enumerated for review. Scanner failures, missing
architectures, invalid reports, stale databases and expired exceptions fail closed.

## Build, scan, sign, promote

The repository-owned Docker workflow builds amd64 and arm64 natively and pushes
content-addressed candidates. A run-specific `audit-*` tag assembles the candidate
index; it is not a release channel. Both architecture manifests are catalogued and
scanned. Only after the gate passes is the candidate digest signed and promoted to
the existing edge/SHA, beta/version, or stable/version/latest tags. Promotion uses
the scanned index rather than rebuilding. Public release tags stay unchanged on
scan or signing failure. Pull requests build and scan local archives without registry
write permissions or secrets. All external CI references remain immutable.

The scanner receives read-only image archives/SBOMs, not Docker credentials. A
single vulnerability database is used for the complete multi-architecture audit.
Its validated status, checksummed source URL and build time are retained. It must
be no older than 24 hours. A supplied `--database` cache permits before/after scans
against the identical database. Audit artifacts retain raw CycloneDX SBOMs, package
inventories, vulnerability reports, policy decisions and evidence for 90 days.
The daily Image Security workflow resolves edge and the latest versioned release
to immutable digests before scanning; failures remain failed workflow runs.

```bash
python -m scripts.audit_release_image \
  ghcr.io/s3ntin3l8/runway@sha256:<index-digest> --output image-audit
python -m scripts.audit_release_image --archive /tmp/candidate.tar \
  --architecture amd64 --output candidate-audit
```

## Applicability exceptions

`image-exceptions.json` retains raw findings and records only specific reviewed
exceptions. Each entry needs advisory ID, scanner namespace, package name and exact
version, architecture, owner, HTTPS advisory evidence, a Runway issue, review time,
expiry and an exposure rationale. Maximum lifetime is 30 days. Version upgrades,
new advisories, other architectures or expired entries do not inherit an exception.
An exception is a time-limited deployment decision, not a claim that the installed
library is patched. Changes to runtime behavior require re-review even when the
package version is unchanged. Review the exception document in the same PR as the
image change. Never add a wildcard, blanket severity suppression or `only-fixed` filter.

Exceptions apply to the documented non-root server runtime: no privileged container,
Docker socket, arbitrary SQL executor, uploaded SQLite databases, Kerberos acceptor,
DTLS service, GnuTLS networking, or runtime package installation. The final image
removes setuid/setgid permissions from `/usr`. Running a root/privileged container
or extending this runtime invalidates the corresponding exposure assessment.

The source/vendor advisory is authoritative for the affected component. Scanner
severity and fix status are retained even when Debian calls an issue minor or
postpones a security update. Neither label alone is enough to grant an exception.
