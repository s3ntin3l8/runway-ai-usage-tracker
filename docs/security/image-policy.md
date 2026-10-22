# Release image security policy

Runway blocks image promotion on every Critical/High Grype match unless an exact,
reviewed applicability exception exists. Whether the scanner reports a fix does
not change this rule. Medium, Low, Negligible and Unknown findings remain visible;
Python findings are separately enumerated for review. Scanner failures, missing
architectures, invalid reports and stale databases fail closed.

An expired exception fails closed too, but only for the finding it covered. A
lapsed entry applies to nothing, so the Critical/High match it excepted becomes
unexcepted and blocks promotion, with the advisory and the expiry date named in
the audit evidence. The rest of the exception document keeps working: one lapsed
entry no longer invalidates the others. A malformed document — a missing field,
an unsupported architecture, non-HTTPS evidence, a duplicate entry, a review
dated in the future or a window longer than 30 days — still fails the whole
audit, because a broken document is a different failure class from a lapsed one.

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
Its validated status, checksummed source URL and build time are retained. The
audit uses the release upstream currently advertises, verified by checksum
against that release's source archive, and that release must be no older than 30
hours. Upstream publishes about once a day and a release reaches its download
location roughly an hour after its build stamp, so the newest release anybody
can obtain is up to about 25 hours old by that stamp; the 30 hour ceiling is
the tripwire for an upstream feed that stopped publishing, not a licence to
scan with a database upstream has already superseded. A supplied `--database`
cache permits before/after scans against the identical database. Audit artifacts
retain raw CycloneDX SBOMs, package inventories, vulnerability reports, policy
decisions and evidence for 90 days. The daily Image Security workflow resolves
edge and the latest versioned release to immutable digests before scanning;
failures remain failed workflow runs.

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

The document has a single expiry horizon: every entry expires at the same instant,
so the whole document is re-reviewed on one cycle and there is one deadline to
track. A renewal PR re-reviews the applicability rationale and moves that horizon.
The repository test suite fails when an entry is within seven days of expiry or
already lapsed, naming the entries that need renewal — so the renewal happens
during working hours instead of when an image build turns red.

The exact package version is part of the match, and the runtime version moves with
the base image: the Dockerfile runs `apt-get upgrade -y`, so a new Debian point
release changes the installed version and invalidates the matching exception.
Never widen a match to cover it — re-review and pin the new version instead.

Exceptions apply to the documented non-root server runtime: no privileged container,
Docker socket, arbitrary SQL executor, uploaded SQLite databases, Kerberos acceptor,
DTLS service, GnuTLS networking, or runtime package installation. The final image
removes setuid/setgid permissions from `/usr`. Running a root/privileged container
or extending this runtime invalidates the corresponding exposure assessment.

The source/vendor advisory is authoritative for the affected component. Scanner
severity and fix status are retained even when Debian calls an issue minor or
postpones a security update. Neither label alone is enough to grant an exception.

## Trusted database caches

`--database` is an operator-only path to a previously prepared, trusted local
cache, for controlled before/after comparisons. It is not a cache supplied by a
web request, sidecar, untrusted PR artifact, or release attachment. Cache status
is provenance metadata, not authentication of an arbitrary SQLite file. Only
reuse caches that you prepared with the pinned scanner and kept under your own
control. The downloaded compressed archive checksum cannot be compared directly
to the decompressed database file; an additional local file hash would be a
corruption diagnostic, not a replacement for this trust boundary. A prepared
cache also records the release upstream advertised when it was prepared; the
audit verifies the cached database against that record, and a cache prepared
before the record existed is checked for age only.

Production daily audits prepare one fresh database and use it for both edge and
the newest versioned release. Every architecture still validates its actual Grype
database descriptor against that source. Each target retains independent evidence;
a failure for edge does not skip the release, and either failure fails the job.
Database preparation or freshness failure records failed evidence for both targets.

## Candidate retention and manual cleanup

Keep `audit-RUN-ATTEMPT` candidate references for at least 90 days, matching audit
artifact retention. Canonical edge, SHA, version, beta, stable and latest tags,
retained image indexes, and signature/attestation material take precedence over
this minimum. Age alone never authorizes deleting a registry version.

Cleanup is manual and reviewed. Inventory all tags and retained index references
before considering an old audit-only version. Delete a package version only when
it is proven unreferenced by canonical tags, retained indexes and signature or
attestation material. A version shared with any retained reference must stay.
If the registry inventory cannot prove that a manifest is unreferenced, retain it.
Keep the reviewed inventory and proposed deletions with the maintenance record.

Do not assume removing an audit tag removes the per-architecture bytes pushed by
digest, and do not invent a `docker buildx imagetools rm` command. This policy adds
no automatic deletion and performs no registry cleanup as part of publishing.
