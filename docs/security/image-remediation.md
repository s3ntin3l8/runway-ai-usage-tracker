# Image remediation

The state of the server image's vulnerability review: what the last recorded
scan of the published image found, and why each Critical/High advisory that
remains does not apply to the Runway runtime. The live exception list is
[`image-exceptions.json`](image-exceptions.json) and is what the release gate
enforces; this document is the narrative behind it, and
[`image-policy.md`](image-policy.md) is the policy. Earlier scans, the Bookworm
baseline and the dated re-reviews are in
[`../evidence/image-remediation-history.md`](../evidence/image-remediation-history.md).

This is not a zero-vulnerability claim. Scanner matches are not proven
exploitability, and each figure belongs to one build scanned against one
scanner database; counts from different scans are not comparable.

## The image

The server image is built from `python:3.12-slim-trixie` (Debian 13) with a
`node:24-trixie-slim` frontend builder, both pinned by digest in the
`Dockerfile`. It runs as the non-root `runway` user (UID 1000), strips
setuid/setgid permissions from `/usr`, sets `LANG=C.UTF-8`, and carries no curl,
pip, setuptools or wheel. The dependency builder stage installs its compiler
unpinned as insurance for an sdist-only dependency; every requirement resolves
to a wheel today. Runtime distro packages remain eligible for security upgrades,
and every candidate is scanned by immutable digest before promotion.

The exception rationales rest on these runtime invariants: no privileged
container, added capability, Docker socket mount or custom user-mount entry; no
arbitrary SQL executor, FTS5 table or uploaded SQLite file; no Kerberos, DTLS
or GnuTLS network service; no runtime package installation. Changing any of them
invalidates the corresponding assessment.

## Recorded scan

### The published Debian 13 image

The scan fixture that the exception document is checked against
(`tests/fixtures/image-exceptions/published-release.json`) records this image.
It is an `edge` index, immutable by digest: the rolling tag moves with every
push, the digest does not.

| Reference | Digest |
| --- | --- |
| Published index (`edge`) | `sha256:3a6ff61a7bdcaa67d267692ec1d0af2d370fd6b19e57edae0b78afde4f187b77` |
| linux/amd64 manifest | `sha256:d0468026d177a4b1db0ec0f3eb7e4c6b344b46530aa7b480582e157325f85bff` |
| linux/arm64 manifest | `sha256:4e375b64126b62673355ed78e0328c632690e456a283c1f40ce6453cf584d7e5` |

The platform rows are the manifests the index points at, which are the ones the
audit scans; they are not the per-platform build outputs recorded in the
`digests-*` artifacts.

| | amd64 | arm64 |
| --- | --- | --- |
| passed | true | true |
| blocked | 0 | 0 |
| expired exceptions | 0 | 0 |
| Critical | 0 | 0 |
| High | 61 | 61 |
| Medium | 69 | 69 |
| Low | 12 | 12 |
| Negligible | 47 | 47 |

Scanned at `2026-11-06T22:05:14Z` against the database built
`2026-11-06T06:21:59Z`. Every High match is covered by an exact, per-architecture
exception. Regenerate the fixture with `python -m scripts.record_image_scan` in
the same change that re-reviews the exception document (see
[`image-policy.md`](image-policy.md)); when it moves, record the new index and
architecture digests here.

## Advisories that remain

All entries share one expiry horizon and are re-reviewed together. Rows marked
*vendor will-not-fix* never clear on their own: Debian has decided not to fix
them, so they block if they ever lapse and need a real review at every renewal.
Nothing here is claimed patched; each decision is time-limited.

| Advisory | Packages | Why the runtime is not exposed |
| --- | --- | --- |
| CVE-2025-69720 | `libncursesw6`, `libtinfo6`, `ncurses-base`, `ncurses-bin` | The flaw is in the `infocmp` CLI parser, which Runway never invokes. |
| CVE-2026-102010, CVE-2026-95619 | `gcc-14-base`, `libgcc-s1`, `libstdc++6` | libstdc++ `priority_queue` erase_if and aligned `operator new`; the server has no C++ input path and does not load the C++ runtime. |
| CVE-2026-119373, CVE-2026-119374 (vendor will-not-fix) | `libsqlite3-0` | Need an FTS5 table. Runway defines none, runs no `MATCH` query and opens no SQLite file but its own. |
| CVE-2026-119379 (vendor will-not-fix) | `libsqlite3-0` | Needs `sqlite3changeset_apply()` from the session extension, which Python's `sqlite3` does not expose. |
| CVE-2026-19499 | `libc6`, `libc-bin` | glibc `strfmon` width padding; Runway calls neither `strfmon` nor native monetary formats. |
| CVE-2026-5435 | `libc6`, `libc-bin` | Deprecated DNS record printing APIs (`ns_printrr` and friends); Runway resolves hosts through socket APIs. |
| CVE-2026-84243 (vendor will-not-fix) | `libc6`, `libc-bin` | `gettext` honouring a `LANGUAGE` entry containing `/` or `..`; applies only when `LC_MESSAGES` is not `C`/`POSIX`, and the image fixes `LANG=C.UTF-8` in a deployment-controlled environment with no SSH or `AcceptEnv`. |
| CVE-2026-54369, CVE-2026-54370 | `libacl1` | Need a privileged caller using libacl on attacker-controlled paths; Runway runs as UID 1000 and invokes no ACL tools. |
| CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 | util-linux family: `bsdutils`, `libblkid1`, `liblastlog2-2`, `libmount1`, `libsmartcols1`, `libuuid1`, `login`, `mount`, `util-linux` | Need privileged `mount`/`nsenter` operations on attacker-controlled filesystem or namespace state; Runway has no such path, runs as UID 1000 without setuid binaries, and privileged containers or a Docker socket mount are outside the exception. |
| CVE-2026-82560, CVE-2026-9538 | `perl-base` | Perl `Pod::Text` and `Archive::Tar`; the server executes no Perl. |
| CVE-2026-85091, CVE-2026-76844 | `zlib1g` | Native `gzwrite`/`gz_write` paths; the server uses Python `zlib` and neither `gzip` nor `ctypes`. |

Debian 13 folded the separate `login` source package and the `lastlog` library
into util-linux, so `login` and `liblastlog2-2` belong to that family.

Advisories whose packages are fixed in trixie or absent from the slim runtime
have no exception: an exception for a fixed or absent package is a false record,
and the document is checked for equality with the scan, so an entry without a
finding fails the unit suite. The applicability reasoning for those, and the
Bookworm decisions they replaced, are in the history record.

## Verification

Publishing signs the audited index with the shared Docker publishing workflow's
identity; the commands are in [`../SECURITY.md`](../SECURITY.md). The retained
`release-image-audit-<image-slug>` artifact holds the raw SBOMs, package
inventories and unfiltered scanner reports for both architectures.
