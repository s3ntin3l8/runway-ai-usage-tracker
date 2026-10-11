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

Builder inputs retain exact direct package pins: `gcc=4:12.2.0-3` and
`libsqlite3-dev=3.40.1-2+deb12u2`, verified against Bookworm's resolver on
2026-10-10. These packages stay in the dependency builder stage; pip, setuptools
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
