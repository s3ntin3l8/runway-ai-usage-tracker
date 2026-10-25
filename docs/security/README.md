# Security documentation

Two things live here, and they are different in kind:

- **`docs/SECURITY.md`** is the **trust model and policy** — what Runway trusts, what it verifies, and what an operator must configure. It is a living reference.
- **`docs/security/`** is the **release-image policy and the dated validation evidence** behind it. The evidence is append-only and dated; the policy is enforced by CI.

## Policy

| File | What it covers |
|---|---|
| [`image-policy.md`](image-policy.md) | The release-image gate: what is blocked, what an exception has to look like, and how the exception file is reviewed. |
| [`image-remediation.md`](image-remediation.md) | The narrative of the base-image remediation — what was found, what was fixed, and how each Critical/High advisory was judged. |
| [`image-exceptions.json`](image-exceptions.json) | **The live exception list.** This is the file the release gate actually enforces. 14 CVEs. |

## Validation

| File | What it covers |
|---|---|
| [`physical-validation.md`](physical-validation.md) | The full physical acceptance runbook — browser, native installer, update, restart and rollback QA on real machines. |
| [`release-acceptance.md`](release-acceptance.md) | What has to pass before a release is promoted. |
| [`windows-browser-helper.md`](windows-browser-helper.md) | The Windows DNS helper: what it binds, what it is allowed to serve, and how it is restricted to one client. |

## Evidence

Dated, sanitized records. Each directory is one validation run.

| Directory | Run |
|---|---|
| [`evidence/603-authentik-traefik-2026-10-12/`](evidence/603-authentik-traefik-2026-10-12/README.md) | The Authentik + Traefik TLS matrix (2026-10-12): versions, sanitized configuration, outcomes, immediate-peer logs and teardown. |
| [`evidence/603-macos-2026-10-15/`](evidence/603-macos-2026-10-15/README.md) | The macOS physical validation run (2026-10-15). |
| [`evidence/windows-validation-2026-10-12.md`](evidence/windows-validation-2026-10-12.md) | The Windows validation record (2026-10-12). |
| [`evidence/windows-update-recovery-2026-10-13.md`](evidence/windows-update-recovery-2026-10-13.md) | The Windows update-recovery record (2026-10-13). |

The evidence is **sanitized**: synthetic secrets, no operator credentials, and
private network addresses replaced with RFC 5737 documentation ranges.

## Related

- [`../SECURITY.md`](../SECURITY.md) — the trust model and policy.
- [`../security-validation.md`](../security-validation.md) — how the security gates work, and the dated results of the last run.
- [`../ci-cd.md`](../ci-cd.md) — the workflows these gates live in.
- [`../forward-auth.md`](../forward-auth.md) — the SSO / forward-auth trust boundary.
