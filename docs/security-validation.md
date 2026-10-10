# Security audit validation

This remediation continues [#600](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/600) after merged PR #601. Validation uses synthetic secrets, temporary databases/config directories, and isolated services.

Automated checks:

- Durable signature receipts: concurrent claims, new database connections, rotation, expiry, storage failure, ingestion idempotency and credential-manifest replay. Invalid requests also consume valid signatures; retries sign fresh timestamps.
- Update verification: a real published repository signature and public transparency evidence, tampered payloads, wrong identity/issuer, bounded bundles and unavailable trust refresh. The updater verifies before extraction or installation. Frozen Linux CLI verification accepts the real fixture and rejects tampering; Security Validation CI repeats it for Linux tray, Windows and macOS.
- Browser matrix: 15 passing checks across Chromium, Firefox and WebKit for hostile-origin writes, Host rejection, omitted sidecar fleet keys, authenticated network access and remote Vite authentication. Run `npm --prefix webapp run test:security` after installing Playwright browsers.
- Actual nginx: authenticated `auth_request`, stripped identity headers, forged direct headers from an untrusted loopback address, preserved immediate peer and independently signed sidecar ingestion. Run `RUNWAY_PROXY_TEST=1 pytest tests/security/test_real_proxy.py` with an isolated `RUNWAY_CONFIG_DIR` and Docker.
- CI references: external actions and reusable workflows are pinned to commit SHAs; the shared workflows used by Runway also have immutable action references; `python scripts/check_workflow_pins.py` enforces this in CI.
- Fresh Python server/desktop/sidecar tooling and full npm dependency audits reported no known vulnerabilities at validation time. This is separate from the released image inventory.

The Release Image Audit dispatch workflow accepts an immutable Runway image index digest, pulls each architecture through Docker authentication, then passes read-only archives/SBOMs to digest-pinned Syft and Grype containers. It retains CycloneDX SBOMs, package inventories, vulnerability results and scan evidence. Any matches produce a failed audit, with artifacts retained.

The scanned image index was `sha256:fd1e442ff3cb8e505ee0a35bf409c4b4d2aac1092ef5cc0efaed5f47b0e46f40`: amd64 `sha256:bf3d49e01ee7a99c1364225a1e19ab444ba2b621084ac43cc9443b0c0860cc3f`, arm64 `sha256:2252962d646fd78e9736266de102c8de4d01b1f6e1c39d7bcda2a3e124ea80fc`. Each reports 477 vulnerability matches (28 Critical, 147 High, 156 Medium, 28 Low, 100 Negligible, 18 Unknown); 144 have reported fixes. Matches are 429 Debian, 36 binary and 12 Python (pip), not proven exploitability. [#602](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/602) tracks triage, base/OS/tooling refresh and rescanning a replacement release.

Physical browser DNS rebinding/Local Network Access behavior, actual IdP integration, and native installer/update/restart/rollback QA remain tracked in [#603](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/603). Browser engines, synthetic nginx identity and frozen-verifier CI do not substitute for those checks.

Update archive hashing streams 1 MiB chunks, and verification rejects signature bundles above 1 MiB. The existing download path has no explicit byte limit on temporary disk usage; [#605](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/605) tracks download-time archive/bundle budgets sized against the new cross-platform packages.
