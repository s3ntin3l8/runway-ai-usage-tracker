# Image remediation evidence

Baseline: image index `sha256:fd1e442ff3cb8e505ee0a35bf409c4b4d2aac1092ef5cc0efaed5f47b0e46f40`.

The original scan reported 477 matches per architecture (28 Critical, 147 High); 144 reported fixes. Python 3.12.13 and both copies of pip 25.0.1 are replaced/removed. The candidate uses Python 3.12.15, a refreshed Debian Bookworm runtime, no curl, no pip/setuptools/wheel, and no inherited setuid/setgid permissions.

Critical/High applicability decisions below must be reviewed with the exact-version, per-architecture exception entries. This is not a zero-vulnerability claim. Authoritative Debian tracker data was fetched on 2026-10-10; scanner database identity and all raw reports are retained in audit artifacts.

| Advisory | Runtime applicability decision |
| --- | --- |
| [CVE-2025-13151](https://security-tracker.debian.org/tracker/CVE-2025-13151) | The affected GnuTLS/libtasn1 certificate/ASN.1 parsing path is not used by the Runway server. HTTPS uses Python ssl/OpenSSL and cryptography; GnuTLS/libtasn1 are inherited apt dependencies, not runtime network handlers. Runtime package installation is outside this exception. |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | Affected component is the infocmp CLI parser, not terminal rendering generally. Runway is a Python HTTP server and does not invoke infocmp on local or remote inputs. |
| [CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458) | Requires arbitrary attacker-controlled SQL with a very large ORDER BY expression list. Runway executes application-defined queries with bound values and exposes no arbitrary SQL executor; its own database is the only accepted SQLite file. |
| [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | Affected component is libstdc++ binary-heap priority_queue erase_if. Runway has no C++ priority-queue input path and the inherited C++ runtime is not loaded by the server import smoke test. |
| [CVE-2026-107778](https://security-tracker.debian.org/tracker/CVE-2026-107778) | Requires a Kerberos GSS-API acceptor processing forwarded KRB-CRED arrays. Runway consumes forward-auth HTTP headers; it implements no Kerberos/GSS acceptor and does not load these libraries for server authentication. |
| [CVE-2026-11822](https://security-tracker.debian.org/tracker/CVE-2026-11822) | Requires attacker-supplied malformed SQLite FTS5 pages and execution of an FTS5 MATCH query. Runway creates its own database, has no SQLite upload/import endpoint, and defines no FTS5 tables or MATCH queries. |
| [CVE-2026-11824](https://security-tracker.debian.org/tracker/CVE-2026-11824) | Requires attacker-supplied malformed SQLite FTS5 pages and execution of an FTS5 MATCH query. Runway creates its own database, has no SQLite upload/import endpoint, and defines no FTS5 tables or MATCH queries. |
| [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499) | Advisory affects glibc 2.38 through 2.44 strfmon width-padding paths; this image carries glibc 2.36 and Runway does not call strfmon or accept native monetary formats. |
| [CVE-2026-41992](https://security-tracker.debian.org/tracker/CVE-2026-41992) | Requires invoking GNU gzip -d on crafted LZW then LZH files in one process. Runway does not invoke the gzip executable; HTTP compression uses Python zlib. |
| [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435) | Requires deprecated DNS record printing APIs ns_printrrf/ns_printrr/fp_nquery. Runway resolves hosts through socket APIs and does not print attacker-controlled DNS records through these functions. |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | Requires a privileged caller using libacl pathname functions on attacker-controlled paths. Runway runs as UID 1000 and invokes no libacl/setfacl/getfacl/chacl operations; inherited setuid/setgid permissions are removed. |
| [CVE-2026-54370](https://security-tracker.debian.org/tracker/CVE-2026-54370) | Requires privileged setfacl/chacl/libacl operations racing attacker-controlled paths. Runway runs as UID 1000, invokes none of these tools/APIs, and inherited setuid/setgid permissions are removed. |
| [CVE-2026-5450](https://security-tracker.debian.org/tracker/CVE-2026-5450) | Requires scanf %mc with an explicit width above 1024. Runway processes network inputs in Python/Pydantic and does not pass attacker-controlled native scanf formats to libc. |
| [CVE-2026-5928](https://security-tracker.debian.org/tracker/CVE-2026-5928) | Requires native wide FILE streams and ungetwc with overlapping multibyte encodings. Runway uses Python byte/Unicode I/O, not native wide-character pushback. |
| [CVE-2026-67693](https://security-tracker.debian.org/tracker/CVE-2026-67693) | The affected GnuTLS/libtasn1 certificate/ASN.1 parsing path is not used by the Runway server. HTTPS uses Python ssl/OpenSSL and cryptography; GnuTLS/libtasn1 are inherited apt dependencies, not runtime network handlers. Runtime package installation is outside this exception. |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. |
| [CVE-2026-82560](https://security-tracker.debian.org/tracker/CVE-2026-82560) | Requires Perl Pod::Text formatting untrusted POD. The Runway server does not execute Perl or render POD documents; this inherited distro tooling is outside the server input path. |
| [CVE-2026-84782](https://security-tracker.debian.org/tracker/CVE-2026-84782) | Affected path is OpenSSL DTLS retransmission. Runway serves and collects HTTP(S) using TLS-over-TCP; Python ssl does not provide a DTLS endpoint, and Runway has no DTLS code. |
| [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091) | Requires native gzwrite/gzprintf after nonblocking write stalls. Runway uses Python zlib compression/decompression and does not call native gz-file formatted output APIs. |
| [CVE-2026-88647](https://security-tracker.debian.org/tracker/CVE-2026-88647) | GnuTLS hostname verification/Common Name fallback is not used by Runway HTTP clients, which use Python ssl/OpenSSL. Runtime package installation or adding GnuTLS-based clients invalidates this assessment; the installed library is not claimed patched. |
| [CVE-2026-95184](https://security-tracker.debian.org/tracker/CVE-2026-95184) | The affected GnuTLS/libtasn1 certificate/ASN.1 parsing path is not used by the Runway server. HTTPS uses Python ssl/OpenSSL and cryptography; GnuTLS/libtasn1 are inherited apt dependencies, not runtime network handlers. Runtime package installation is outside this exception. |
| [CVE-2026-95209](https://security-tracker.debian.org/tracker/CVE-2026-95209) | The affected GnuTLS/libtasn1 certificate/ASN.1 parsing path is not used by the Runway server. HTTPS uses Python ssl/OpenSSL and cryptography; GnuTLS/libtasn1 are inherited apt dependencies, not runtime network handlers. Runtime package installation is outside this exception. |
| [CVE-2026-95210](https://security-tracker.debian.org/tracker/CVE-2026-95210) | The affected GnuTLS/libtasn1 certificate/ASN.1 parsing path is not used by the Runway server. HTTPS uses Python ssl/OpenSSL and cryptography; GnuTLS/libtasn1 are inherited apt dependencies, not runtime network handlers. Runtime package installation is outside this exception. |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | Requires Perl Archive::Tar parsing untrusted tar headers. Runway does not execute Perl or use Perl archive modules; its sidecar update extraction is outside the server image. |
| [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | Affected path is libstdc++ aligned operator new with large inputs. Runway is Python/Rust/C and has no C++ aligned-allocation interface processing user input; the inherited C++ runtime is not loaded by the server import smoke test. |

## Validation and closure

Before closure, attach the original and replacement audit artifacts, record both replacement architecture digests and image index digest, and confirm public promotion passed the gate. Keep #602 open until the replacement image has actually been published; a source PR alone does not replace released bytes.

Evidence collected during implementation includes source searches for FTS/MATCH/arbitrary SQL, native scanf/wide I/O, GSSAPI, GnuTLS, DTLS and subprocess usage, and a non-root candidate import/runtime-library inventory. The standard server smoke check must start with isolated data and exercise `/api/v1/system/health`. Full tests remain required to verify application behavior after the base refresh.

Builder inputs retain exact direct package pins: `gcc=4:14.2.0-1` and
`libsqlite3-dev=3.46.1-7+deb13u2`, verified against Debian 13's resolver on
2026-10-23. These packages stay in the dependency builder stage; pip, setuptools
and wheel are removed there before copying the venv. The final stage separately
removes the base interpreter's packaging tools. Runtime distro packages remain
eligible for security upgrades; every resulting candidate is scanned by immutable
digest before promotion. If signing fails, publishing remains failed and operators
must re-run the complete build/audit/sign path; audit tags remain as diagnostic
references and are not public release tags.

## Published beta.8 replacement

Release source: `4c784e73d59b753be209aef8b6765c91787603cd`.
[Release Please run 38128962870](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/38128962870)
passed native builds, the two-architecture audit, candidate signing and promotion
of exactly the audited index to `3.0.0-beta.8` and `beta`.

| Reference | Digest |
| --- | --- |
| Published index | `sha256:e0699f280795b0b39ff617be3d4bbc2dc7852e24e783072d1b8c7ed6bd8724b6` |
| linux/amd64 | `sha256:8c556a77ae050c106e731d908eb2e2f9856dcdbe07885c43d2c101226a264332` |
| linux/arm64 | `sha256:552474695c26a06e841aab151ced9936aa75fcf0947e37f3538df778f86033c4` |

The retained `release-image-audit` artifact reports 279 matches per architecture:
5 Critical, 66 High, 93 Medium, 17 Low, 81 Negligible, 17 Unknown. All 71
Critical/High matches have exact reviewed exceptions; neither architecture has a
blocked match or Python finding. The database was built at
`2026-10-11T06:30:42Z`, source compressed archive checksum
`a1e7eb21958172e46e2bb6db02e72d68eb4efd4b17d0c43fba5e56ae73d0a196`.

The original baseline retained 477 matches per architecture, including 175
Critical/High and 12 Python findings. The earlier controlled before/after scans
used the same database; the published replacement above uses a fresh database and
includes the newly reported CVE-2026-88647. These counts are scanner matches, not
a claim of zero vulnerabilities. See [release acceptance](release-acceptance.md)
for captured sidecar validation and the evidence required for issue closure.

Independent verification of the public index passed with Cosign 3.1.3, container
`ghcr.io/sigstore/cosign/cosign@sha256:9e5c2f2edc34351160407ca3416c61855bdf9403c3c5936e0f0be7fc261611b8`,
requiring certificate identity
`https://github.com/s3ntin3l8/runway-ai-usage-tracker/.github/workflows/docker-publish.yml@refs/heads/main`
and issuer `https://token.actions.githubusercontent.com`. Certificate chain,
transparency evidence and index digest claims were checked. A separate registry
inspection confirmed the public versioned tag matches the index above.

## Util-linux applicability review, 2026-10-15

[Issue #640](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/640)
tracks two new High advisories blocking both candidate architectures in
[run 38774514983](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/38774514983).
The original unfiltered reports and policy decisions remain in the
`image-security-amd64` and `image-security-arm64` artifacts. Each has 16 blocked
matches across eight util-linux binary packages, with no Python findings.

Debian Bookworm's installed `2.38.1-5+deb12u3` remains vulnerable; `bsdutils`
includes epoch `1:`. The Debian tracker lists fixes in newer distribution
versions, not a Bookworm update at review time. These exact package/version and
architecture assessments expire after seven days, on 2026-10-22; #640 remains
open to reassess the upstream fix and applicability before expiry.

| Advisory | Runtime applicability decision |
| --- | --- |
| [CVE-2026-53613](https://security-tracker.debian.org/tracker/CVE-2026-53613), [upstream](https://github.com/util-linux/util-linux/security/advisories/GHSA-8gj5-72r3-428g) | Exploitation needs a privileged mount path and a user/users fstab entry whose target has an attacker-writable ancestor. The Dockerfile runs as UID 1000 and removes inherited setuid/setgid permissions; the server invokes no mount/libmount operations. Privileged containers, additional capabilities, a host Docker socket, or custom user-mount fstab entries invalidate this assessment. |
| [CVE-2026-53615](https://security-tracker.debian.org/tracker/CVE-2026-53615), [upstream](https://github.com/util-linux/util-linux/security/advisories/GHSA-h4rw-gv36-wmp5) | The affected path probes crafted DOS extended partitions through libblkid. The server accepts usage/configuration data and has no blkid/libblkid calls, disk-image upload/probing, block-device input, or downstream partition consumer. Adding those operations invalidates this assessment. |

Source inspection covered `app/`, its direct requirements, the Dockerfile and
the supplied Compose configuration. Static SPA mounting is an HTTP route, not a
filesystem mount. The exceptions do not assert these packages are patched or
cover deployments with different privileges or input paths. The existing
High/Critical gate, 30-hour database limit, unfiltered report retention and
per-version/per-architecture matching remain in force.

An isolated local amd64 candidate built from source `66bd9a52` passed the server
import check with no network, a read-only root and temporary configuration under
`/tmp`. Its inventory showed UID 1000, no active fstab entries, no inherited
setuid/setgid files, and no loaded libmount/libblkid libraries. Installed util-linux
and library versions matched the scanned versions above. This is an amd64
runtime observation; fresh CI scans independently check both architectures.
Re-evaluating both retained unfiltered reports with the proposed exceptions
accepted exactly the 16 new matches per architecture; changing one affected
package to an unreviewed version remained blocked.

## Util-linux applicability review, 2026-10-22

[Issue #799](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/799)
records that the 32 `util-linux` exceptions above lapsed at
`2026-10-22T17:14:08Z` and took every image build with them: the loader
rejected the whole document on the first expired entry, so the 142 entries that
were still valid never got to apply.

The Debian tracker was re-read on 2026-10-22 for both advisories. Bookworm ships
`2.38.1-5+deb12u3` and remains vulnerable; so does bookworm-security at
`2.38.1-5+deb12u1`. The upstream fix ships in trixie at `2.41.5-0+deb13u1` and in
sid/forky at `2.42.4-1`. There is no fixed Bookworm point release, so the
runtime version in the image cannot be moved forward by an upgrade, and the
exception is still required: the affected packages are still present and the
advisories are still reported as High and not-fixed.

The exposure rationale was re-checked against the current tree. The server
still invokes no `mount`, `libmount`, `losetup`, `blkid` or `fstab` path — the
only `mount` call in `app/` is FastAPI's HTTP route for static assets — and
still has no file-upload, disk-image or block-device input path. The Dockerfile
still runs as UID 1000 and strips inherited setuid/setgid permissions from
`/usr`, and neither compose file gives the `runway` service a privileged
container, an added capability or a host Docker socket.

The 32 entries were therefore renewed with a fresh review time and the same
`2026-11-08T12:00:00Z` expiry the rest of the document already carries, so the
whole exception document now has one horizon and one renewal cycle. The loader
was changed at the same time: an expired entry now applies to nothing and only
blocks the finding it covered, naming the advisory and the expiry date, instead
of invalidating every other entry with it. A malformed document still fails the
whole audit. The repository test suite now fails when an entry is within seven
days of expiry, and the daily `Image Security` run carries the same check, so
the next renewal is a scheduled task rather than a build outage even when no
pull request is open. The exception document is in both image-security path
filters, so a renewal is scanned like any other change to the policy.

## Debian 13 (trixie) base, 2026-10-23

[Issue #805](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/805) moves the server image off Debian Bookworm. Bookworm ships `util-linux 2.38.1-5+deb12u3` and has no fixed point release for the advisories that affect it, so `apt-get upgrade -y` cannot move the runtime off the vulnerable version and the exception would have been renewed indefinitely on a 30-day cycle. Debian 13 (trixie) ships the fixed `2.41.5-0+deb13u1`.

| Reference | Digest |
| --- | --- |
| `python:3.12-slim-trixie` (builder and runtime) | `sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704` |
| `node:24-trixie-slim` (frontend builder) | `sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea` |

Both digests were re-resolved against the registry when this change was authored, and both carry the same toolchain as the Bookworm bases they replace: Python 3.12.15 and Node 24.22.1 (npm 11.19, which `webapp/package.json`'s `devEngines` requires and which preserves the lockfile's `libc` metadata). The Python version stays 3.12: the repository's floor is `>=3.12`, CI runs 3.12 and ruff targets `py312`.

### The candidate scan

The per-architecture candidate scans are the record. The amd64 and arm64 candidates were built from this change and scanned with the same digest-pinned Syft and Grype the release path uses:

| Field | Value |
| --- | --- |
| Syft | `anchore/syft@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa` |
| Grype | `anchore/grype@sha256:e4a44ef45d285b829ce6efe2642980329661bd2d18eab5fc539138d4adaebbbe` |
| Database | built `2026-10-23T06:30:15Z`, status `active` |
| Database checksum | `sha256:6e78c0f757fe96976b37b138e54e83fd75f2ad1f6c5feae2ecb008cefa8fcc0f` |
| Matches per architecture | 166 |
| Severity per architecture | 55 High, 10 Low, 54 Medium, 47 Negligible |
| Critical/High | none Critical; 55 High, each covered by an exact reviewed exception |

Each figure in this document belongs to a specific build against a specific
scanner database, and they are not comparable across builds. The *Published
beta.8 replacement* section above records 279 matches per architecture from the
beta.8 audit, whose database was built `2026-10-11T06:30:42Z`. The published
leg dispatched on `main` after #852 merged — run 39989382509 — audited the
published `edge` image and `3.0.0-beta.11` against the database built
`2026-10-23T06:30:15Z` and reported 283 matches per architecture with 3
Critical and 79 High, every one of them covered by a reviewed exception. The
trixie candidate in this section was audited against that same
`2026-10-23T06:30:15Z` database and reports 166, with no Critical finding at
all.

### Advisories Debian lists as fixed in trixie

These ten advisories no longer apply to the runtime. Their exceptions are removed rather than renewed: an exception for a fixed advisory is not a decision, it is a false record.

| Advisory | Bookworm version it replaced | Fixed in trixie at |
| --- | --- | --- |
| [CVE-2025-13151](https://security-tracker.debian.org/tracker/CVE-2025-13151) | `libtasn1-6` `4.19.0-2+deb12u1` | `4.20.0-2+deb13u1` |
| [CVE-2025-7458](https://security-tracker.debian.org/tracker/CVE-2025-7458) | `libsqlite3-0` `3.40.1-2+deb12u2` | `3.42.0-1` |
| [CVE-2026-11822](https://security-tracker.debian.org/tracker/CVE-2026-11822) | `libsqlite3-0` `3.40.1-2+deb12u2` | `3.46.1-7+deb13u2` |
| [CVE-2026-11824](https://security-tracker.debian.org/tracker/CVE-2026-11824) | `libsqlite3-0` `3.40.1-2+deb12u2` | `3.46.1-7+deb13u2` |
| [CVE-2026-41992](https://security-tracker.debian.org/tracker/CVE-2026-41992) | `gzip` `1.12-1` | `1.13-1+deb13u1` |
| [CVE-2026-53613](https://security-tracker.debian.org/tracker/CVE-2026-53613) | `util-linux` `2.38.1-5+deb12u3` | `2.41.5-0+deb13u1` |
| [CVE-2026-53615](https://security-tracker.debian.org/tracker/CVE-2026-53615) | `util-linux` `2.38.1-5+deb12u3` | `2.41.5-0+deb13u1` |
| [CVE-2026-5450](https://security-tracker.debian.org/tracker/CVE-2026-5450) | `libc6` `2.36-9+deb12u14` | `2.41-12+deb13u4` |
| [CVE-2026-5928](https://security-tracker.debian.org/tracker/CVE-2026-5928) | `libc6` `2.36-9+deb12u14` | `2.41-12+deb13u4` |
| [CVE-2026-84782](https://security-tracker.debian.org/tracker/CVE-2026-84782) | `openssl` `3.0.22-1~deb12u1` | `3.5.7-1~deb13u3` |

### Advisories whose packages left the image

Debian 13's slim runtime does not carry these packages at all, so the advisories cannot fire against it. Their exceptions are removed for the same reason as the fixed ones: an exception for something the image does not contain is not a decision.

| Advisory | Package that left the image | Source package |
| --- | --- | --- |
| [CVE-2026-107778](https://security-tracker.debian.org/tracker/CVE-2026-107778) | `libgssapi-krb5-2, libk5crypto3, libkrb5-3, libkrb5support0` | `krb5` |
| [CVE-2026-67693](https://security-tracker.debian.org/tracker/CVE-2026-67693) | `libgnutls30t64` | `gnutls28` |
| [CVE-2026-88647](https://security-tracker.debian.org/tracker/CVE-2026-88647) | `libgnutls30t64` | `gnutls28` |
| [CVE-2026-95184](https://security-tracker.debian.org/tracker/CVE-2026-95184) | `libgnutls30t64` | `gnutls28` |
| [CVE-2026-95209](https://security-tracker.debian.org/tracker/CVE-2026-95209) | `libgnutls30t64` | `gnutls28` |
| [CVE-2026-95210](https://security-tracker.debian.org/tracker/CVE-2026-95210) | `libgnutls30t64` | `gnutls28` |

### Advisories that remain, with the trixie runtime versions

| Advisory | Trixie runtime package and version | Runtime applicability decision |
| --- | --- | --- |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | `libncursesw6` `6.5+20250216-2`, `libtinfo6` `6.5+20250216-2`, `ncurses-base` `6.5+20250216-2`, `ncurses-bin` `6.5+20250216-2` | Affected component is the infocmp CLI parser, not terminal rendering generally. Runway is a Python HTTP server and does not invoke infocmp on local or remote inputs. |
| [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010) | `gcc-14-base` `14.2.0-19`, `libgcc-s1` `14.2.0-19`, `libstdc++6` `14.2.0-19` | Affected component is libstdc++ binary-heap priority_queue erase_if. Runway has no C++ priority-queue input path and the inherited C++ runtime is not loaded by the server import smoke test. |
| [CVE-2026-19499](https://security-tracker.debian.org/tracker/CVE-2026-19499) | `libc-bin` `2.41-12+deb13u4`, `libc6` `2.41-12+deb13u4` | Advisory affects glibc 2.38 through 2.44 strfmon width-padding paths; this image carries glibc 2.41, inside the affected range, and Runway does not call strfmon or accept native monetary formats. |
| [CVE-2026-5435](https://security-tracker.debian.org/tracker/CVE-2026-5435) | `libc-bin` `2.41-12+deb13u4`, `libc6` `2.41-12+deb13u4` | Requires deprecated DNS record printing APIs ns_printrrf/ns_printrr/fp_nquery. Runway resolves hosts through socket APIs and does not print attacker-controlled DNS records through these functions. |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | `libacl1` `2.3.2-2+b1` | Requires a privileged caller using libacl pathname functions on attacker-controlled paths. Runway runs as UID 1000 and invokes no libacl/setfacl/getfacl/chacl operations; inherited setuid/setgid permissions are removed. |
| [CVE-2026-54370](https://security-tracker.debian.org/tracker/CVE-2026-54370) | `libacl1` `2.3.2-2+b1` | Requires privileged setfacl/chacl/libacl operations racing attacker-controlled paths. Runway runs as UID 1000, invokes none of these tools/APIs, and inherited setuid/setgid permissions are removed. |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | `bsdutils` `1:2.41.5-0+deb13u1`, `libblkid1` `2.41.5-0+deb13u1`, `liblastlog2-2` `2.41.5-0+deb13u1`, `libmount1` `2.41.5-0+deb13u1`, `libsmartcols1` `2.41.5-0+deb13u1`, `libuuid1` `2.41.5-0+deb13u1`, `login` `1:4.16.0-2+really2.41.5-0+deb13u1`, `mount` `2.41.5-0+deb13u1`, `util-linux` `2.41.5-0+deb13u1` | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, the server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, and Debian 13 folded the separate login source package and the lastlog library into util-linux, so the family in this image is bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount and util-linux. The server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | `bsdutils` `1:2.41.5-0+deb13u1`, `libblkid1` `2.41.5-0+deb13u1`, `liblastlog2-2` `2.41.5-0+deb13u1`, `libmount1` `2.41.5-0+deb13u1`, `libsmartcols1` `2.41.5-0+deb13u1`, `libuuid1` `2.41.5-0+deb13u1`, `login` `1:4.16.0-2+really2.41.5-0+deb13u1`, `mount` `2.41.5-0+deb13u1`, `util-linux` `2.41.5-0+deb13u1` | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, the server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, and Debian 13 folded the separate login source package and the lastlog library into util-linux, so the family in this image is bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount and util-linux. The server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | `bsdutils` `1:2.41.5-0+deb13u1`, `libblkid1` `2.41.5-0+deb13u1`, `liblastlog2-2` `2.41.5-0+deb13u1`, `libmount1` `2.41.5-0+deb13u1`, `libsmartcols1` `2.41.5-0+deb13u1`, `libuuid1` `2.41.5-0+deb13u1`, `login` `1:4.16.0-2+really2.41.5-0+deb13u1`, `mount` `2.41.5-0+deb13u1`, `util-linux` `2.41.5-0+deb13u1` | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, the server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, and Debian 13 folded the separate login source package and the lastlog library into util-linux, so the family in this image is bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount and util-linux. The server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | `bsdutils` `1:2.41.5-0+deb13u1`, `libblkid1` `2.41.5-0+deb13u1`, `liblastlog2-2` `2.41.5-0+deb13u1`, `libmount1` `2.41.5-0+deb13u1`, `libsmartcols1` `2.41.5-0+deb13u1`, `libuuid1` `2.41.5-0+deb13u1`, `login` `1:4.16.0-2+really2.41.5-0+deb13u1`, `mount` `2.41.5-0+deb13u1`, `util-linux` `2.41.5-0+deb13u1` | Requires privileged mount/nsenter operations on attacker-controlled filesystem or namespace state. Runway runs as UID 1000, has no mount/nsenter input path or CAP_SYS_ADMIN requirement, and removes inherited setuid/setgid permissions. Privileged containers and host Docker socket mounts are outside this exception. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, the server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. Re-reviewed 2026-10-23 for the Debian 13 (trixie) base: util-linux ships 2.41.5-0+deb13u1, and Debian 13 folded the separate login source package and the lastlog library into util-linux, so the family in this image is bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount and util-linux. The server still invokes no mount or libmount operation and still runs as UID 1000 with setuid/setgid permissions stripped, and the runtime still has no privileged container, added capability, host Docker socket or custom user-mount fstab entry. |
| [CVE-2026-82560](https://security-tracker.debian.org/tracker/CVE-2026-82560) | `perl-base` `5.40.1-6+deb13u1` | Requires Perl Pod::Text formatting untrusted POD. The Runway server does not execute Perl or render POD documents; this inherited distro tooling is outside the server input path. |
| [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091) | `zlib1g` `1:1.3.dfsg+really1.3.1-1+b1` | Requires native gzwrite/gzprintf after nonblocking write stalls. Runway uses Python zlib compression/decompression and does not call native gz-file formatted output APIs. |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | `perl-base` `5.40.1-6+deb13u1` | Requires Perl Archive::Tar parsing untrusted tar headers. Runway does not execute Perl or use Perl archive modules; its sidecar update extraction is outside the server image. |
| [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619) | `gcc-14-base` `14.2.0-19`, `libgcc-s1` `14.2.0-19`, `libstdc++6` `14.2.0-19` | Affected path is libstdc++ aligned operator new with large inputs. Runway is Python/Rust/C and has no C++ aligned-allocation interface processing user input; the inherited C++ runtime is not loaded by the server import smoke test. |

The Debian tracker data behind this table was read on 2026-10-23, the day the change was authored. Every entry in the shipped exception document was then re-checked against the candidate scan: the document holds exactly the Critical/High findings the scanner reports for each architecture - no finding without an entry, and no entry without a finding. The candidate scan that confirms the corrected document green is the one this change's own pull request runs.

Two things about the util-linux family changed shape in Debian 13, and the candidate scan is what showed them: Debian 13 folded the separate `login` source package and the `lastlog` library into util-linux, so `login` and `liblastlog2-2` are now part of the family and carry the same advisories; and `util-linux-extra` is no longer a package, its utilities having moved into `util-linux`. A family carried over from the Bookworm document would have been wrong in both directions.

The runtime invariants the rationales rest on are unchanged by this change - the same Dockerfile lines create UID 1000, strip inherited setuid/setgid permissions from `/usr` and switch to the non-root `runway` user. The isolated local candidate check the earlier sections describe was not repeated in the environment that authored this change, which has no container runtime; the per-architecture `image-audit` artifacts are the record, and they retain the raw SBOM, the package inventory and the unfiltered Grype report for both architectures.

The published replacement digests are recorded when the trixie image is released, in the same shape as the *Published beta.8 replacement* section above. Until then this section describes the base change, the candidate scans and the applicability decisions, not released bytes: keep #805 open until the published image has been audited, signed and promoted.
