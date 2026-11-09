# Security documentation

- **[`../SECURITY.md`](../SECURITY.md)** is the trust model and policy: what
  Runway trusts, what it verifies, and what an operator must configure.
- **`docs/security/`** (this directory) holds the release-image policy and the
  physical acceptance runbook. CI enforces the policy.
- **[`../evidence/`](../evidence/README.md)** holds the dated, sanitized records
  of validation runs, including the security records.

## Policy

| File | What it covers |
|---|---|
| [`image-policy.md`](image-policy.md) | The release-image gate: what is blocked, what an exception has to look like, and how the exception file is reviewed. |
| [`image-remediation.md`](image-remediation.md) | The current image review: the recorded scan of the published image and why each remaining Critical/High advisory does not apply to the runtime. |
| [`image-exceptions.json`](image-exceptions.json) | The live exception list, the file the release gate enforces. |

## Validation

| File | What it covers |
|---|---|
| [`physical-validation.md`](physical-validation.md) | The physical acceptance runbook (native install, update, restart and rollback; browser boundaries; Authentik/Traefik) and the release acceptance checks. |

## Records

Dated records of completed runs and the Windows DNS helper runbook are under
[`../evidence/`](../evidence/README.md#security-validation): the Authentik/Traefik
TLS matrix, the macOS and Windows validation records, the Windows update-recovery
record, [`windows-browser-helper.md`](../evidence/windows-browser-helper.md) and
the [image remediation history](../evidence/image-remediation-history.md).

## Related

- [`../security-validation.md`](../security-validation.md): how the security gates work.
- [`../ci-cd.md`](../ci-cd.md): the workflows these gates live in.
- [`../forward-auth.md`](../forward-auth.md): the SSO / forward-auth trust boundary.
