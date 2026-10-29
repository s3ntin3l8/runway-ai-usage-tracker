# Acceptance and verification records

These are **point-in-time acceptance and verification records** produced by
superpowers-driven PR runs, named after the GitHub issue they evidence
(`<provider>-<topic>-<issue>.{md,json}`). Repeated runs may append a
`-YYYYMMDD` date before the extension to keep earlier records unchanged. A
companion `-observations.json` file may hold sanitized supplementary checks;
the paired report documents its relationship to the primary evidence schema.

They are **not** maintained reference documentation. Do not read them to learn
how the system behaves today — for current behaviour read:

| Question | Read |
|---|---|
| How does provider *N* collect quota? | [`../collectors/`](../collectors/) |
| What does the sidecar do? | [`../sidecar.md`](../sidecar.md) |
| What endpoints exist? | [`../api-reference.md`](../api-reference.md) |
| What is the data model? | [`../architecture.md`](../architecture.md) |
| Which sources may be read, and who grants that? | [`../source-consent.md`](../source-consent.md) |

## What these files contain

Each record captures **booleans, counts, statuses and build hashes** — never
credential values, account identifiers, file contents or transcripts. Tokens,
recovery payloads, keychain profiles and raw ACL snapshots stayed in memory,
private pipes or the OS keychain; the evidence deliberately records only the
*outcome*.

That property is what makes them safe to keep in the repository, and it is why
they are archived rather than deleted: several back **still-open** trackers
(#526, #548, #672, #750, #764) and are the evidence trail for them.

They are **append-only**. Nothing here is edited after the fact; a newer run
produces a newer file.

## Index

### Claude credential sources

| File | Issue | Subject |
|---|---|---|
| [`claude-credential-inventory-663.md`](acceptance/claude-credential-inventory-663.md) / [`.json`](acceptance/claude-credential-inventory-663.json) | #663 | Inventory of Claude credential sources found on a real macOS host |
| [`claude-console-native-investigation-666.md`](acceptance/claude-console-native-investigation-666.md) / [`.json`](acceptance/claude-console-native-investigation-666.json) | #666 | Native investigation of the Claude Console credential path |
| [`claude-cloud-accounting-669.md`](acceptance/claude-cloud-accounting-669.md) | #669 | Offline accounting audit of the Claude cloud usage interface |
| [`claude-cloud-verification-669.md`](acceptance/claude-cloud-verification-669.md) | #669 | Live-verification gates for the cloud accounting audit |
| [`claude-cloud-mapped-fixture-669-20261026.md`](acceptance/claude-cloud-mapped-fixture-669-20261026.md) / [`.json`](acceptance/claude-cloud-mapped-fixture-669-20261026.json) / [observations](acceptance/claude-cloud-mapped-fixture-669-20261026-observations.json) | #669, #992, #993 | One mapped Desktop Code fixture: input/cache parity passed; output, coverage and provenance remain unverified |
| [`claude-cloud-source-shape-20261029.md`](acceptance/claude-cloud-source-shape-20261029.md) / [`.json`](acceptance/claude-cloud-source-shape-20261029.json) | #669, #993 | Structure-only capture of real cloud-runner list rows and three event histories: fields available (repo, branches, per-message model/usage, cwd), the three usage sources and why they disagree |
| [`claude-automatic-native-acceptance-671.json`](acceptance/claude-automatic-native-acceptance-671.json) / [`.md`](acceptance/claude-automatic-native-acceptance-671.md) | #671 | Acceptance of automatic native credential discovery |
| [`claude-native-macos-acceptance-672.json`](acceptance/claude-native-macos-acceptance-672.json) / [`.md`](acceptance/claude-native-macos-acceptance-672.md) | #672 | Acceptance for the remaining macOS Claude credential sources |
| [`claude-native-oauth-acceptance-679.json`](acceptance/claude-native-oauth-acceptance-679.json) / [`.md`](acceptance/claude-native-oauth-acceptance-679.md) | #679 | Acceptance of the Claude Desktop OAuth path |

### Keychain safety, isolation and renewal

| File | Issue | Subject |
|---|---|---|
| [`claude-keychain-renewal-native-526.json`](acceptance/claude-keychain-renewal-native-526.json) / [`.md`](acceptance/claude-keychain-renewal-native-526.md) | #526 | Native renewal/write-back behaviour on macOS Keychain |
| [`claude-keychain-tray-acceptance-548.json`](acceptance/claude-keychain-tray-acceptance-548.json) / [`.md`](acceptance/claude-keychain-tray-acceptance-548.md) | #548 | Tray build keep-alive acceptance, including the read-boundary proof |
| [`claude-keychain-authorization-767.md`](acceptance/claude-keychain-authorization-767.md) | #767 | Authorization path for the foreground helper |
| [`claude-keychain-renewal-followup-767.json`](acceptance/claude-keychain-renewal-followup-767.json) / [`.md`](acceptance/claude-keychain-renewal-followup-767.md) | #767 | Renewal follow-up measurements |
| [`claude-keychain-isolation-773.json`](acceptance/claude-keychain-isolation-773.json) / [`.md`](acceptance/claude-keychain-isolation-773.md) | #773 | Isolation between creator and helper processes |
| [`claude-keychain-isolation-control-773.md`](acceptance/claude-keychain-isolation-control-773.md) | #773 | Negative control for the isolation probe |
| [`claude-keychain-authorization-feasibility-773.json`](acceptance/claude-keychain-authorization-feasibility-773.json) | #773 | Feasibility of the supported write path |
| [`claude-keychain-control-ci-773.json`](acceptance/claude-keychain-control-ci-773.json) | #773 | Negative control in CI |
| [`claude-keychain-creator-metadata-ci-773.json`](acceptance/claude-keychain-creator-metadata-ci-773.json) | #773 | Creator-metadata-only CI control |
| [`claude-keychain-helper-authorization-773.md`](acceptance/claude-keychain-helper-authorization-773.md) | #773 | Helper-side authorization result |
| [`claude-keychain-helper-owned-ci-773.json`](acceptance/claude-keychain-helper-owned-ci-773.json) | #773 | Helper-owned CI control |
| [`claude-keychain-one-time-authorization-773.md`](acceptance/claude-keychain-one-time-authorization-773.md) | #773 | One-time authorization semantics |
| [`claude-keychain-policy-probe-773.json`](acceptance/claude-keychain-policy-probe-773.json) | #773 | Policy probe against the supported write path |
| [`claude-keychain-public-update-probe-773.md`](acceptance/claude-keychain-public-update-probe-773.md) | #773 | Public update control probe (metadata only) |
| [`claude-keychain-public-update-ci-849.json`](acceptance/claude-keychain-public-update-ci-849.json) | #849 | Public update CI control |
| [`claude-keychain-reference-ci-773.json`](acceptance/claude-keychain-reference-ci-773.json) | #773 | Reference CI run for the isolation suite |
| [`claude-keychain-writer-read-ci-773.json`](acceptance/claude-keychain-writer-read-ci-773.json) | #773 | Writer/read separation CI run |

### macOS prompt repair and server memory

| File | Issue | Subject |
|---|---|---|
| [`macos-keychain-prompt-repair-764.md`](acceptance/macos-keychain-prompt-repair-764.md) | #764 | Prevention of repeated Keychain approval prompts |
| [`macos-keychain-prompt-repair-native-764.json`](acceptance/macos-keychain-prompt-repair-native-764.json) | #764 | Native measurements for the prompt repair |
| [`server-memory-results.json`](acceptance/server-memory-results.json) | — | Raw measurements behind [`../server-memory.md`](../server-memory.md) |

## Related

- [`../audit/`](../audit/) — the audit reports and the frozen snapshots.
- [`../security/evidence/`](../security/evidence/) — sanitized security validation evidence.
