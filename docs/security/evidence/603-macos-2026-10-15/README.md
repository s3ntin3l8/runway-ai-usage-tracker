# macOS acceptance: preserved build A — 15 October 2026

Status: **capture complete; physical macOS acceptance pending**. This record
preserves the starting point for #603's separate macOS matrix. It does not report
installation, update, rollback, Gatekeeper or browser acceptance.

## Operator and artifact provenance

The Mac operator reports macOS **26.6.2, arm64** and has preserved immutable local
copies. See the [operator handoff](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/603#issuecomment-6197181873).
The operator verified checksums and used `cosign verify-blob` for the ZIP, DMG and
signed checksum manifest with the exact main signing identity and GitHub issuer.

Dev-01 independently downloaded a second immutable asset set and verified payload
sizes, GitHub asset digests, per-asset checksums, and Sigstore bundles for both
payloads and `SHA256SUMS.txt` using the production verifier. Each certificate's
source-repository-digest extension matches the full A revision. Release ID, asset
IDs/sizes/digests/update timestamps and edge tag stayed unchanged across capture.
No macOS payload was installed or executed on dev-01.

| Field | Recorded value |
| --- | --- |
| A source | `66bd9a52efd941884d98101c7e455f3c242453b2` |
| Publishing workflow | [38768548667 — successful](https://github.com/s3ntin3l8/runway-ai-usage-tracker/actions/runs/38768548667) |
| ZIP | `Runway-Sidecar-macOS-edge.zip` |
| ZIP SHA-256 | `9e929858e8770ea7634285f9d64a57466e53fde11409a62c1de091f0f08bc12a` |
| DMG | `Runway-Sidecar-macOS-edge.dmg` |
| DMG SHA-256 | `c3b35e68f21e29ec5cfb3cf3346790eb89127b8400dcac73b354b112418e3463` |
| Signing identity | `https://github.com/s3ntin3l8/runway-ai-usage-tracker/.github/workflows/sidecar-build.yml@refs/heads/main` |
| OIDC issuer | `https://token.actions.githubusercontent.com` |

Dev-01 retains the ZIP and DMG, both checksum files and bundles, signed
`SHA256SUMS.txt` plus its bundle, and `capture.json` under
`/home/bjoern/.local/share/runway-validation/macos-603/A-66bd9a52/`.
The metadata includes the UTC capture time and exact release/asset identifiers.
The Mac operator's local copies remain independent of this dev-01 set. No private
keys, configuration bodies, profiles or control tokens are part of this record.
Sigstore verification is not Apple notarization.

## Next execution and scope

At A capture, main and edge both resolved to the A revision. Rebuilding the same
commit would not provide the distinct source/version required for B. After this
evidence record is reviewed and merged, publish from then-current approved main
using the normal edge workflow, capture and verify B separately, and report its
full source commit and workflow URL before the Mac begins genuine published-channel
A → B → A → B on both portable ZIP and DMG installation forms. This record does
not claim B has already been published. Inspect actual embedded runtime versions;
do not infer the short-SHA suffix from a handoff description.

Follow [physical-validation.md](../../physical-validation.md) for isolated configs,
owner-state comparisons, human-operated prompts, native lock/failure/refusal rows,
and browser evidence. Source-only refusal fixtures remain separate from genuine
published-channel updates; they do not certify a malformed published release
through the shipped tray. Browser helpers require fresh controlled names,
certificates, exact observed source admission and a coordinated lease; earlier
Windows helpers were torn down. Do not reuse their addresses or armed names.

Completed actual Authentik/Traefik TLS and Windows matrices are retained rather
than repeated. [#631](https://github.com/s3ntin3l8/runway-ai-usage-tracker/pull/631)
completed the full Windows gate follow-up and closed #621, retaining the stated
symlink-privilege skips. Windows Firefox is explicitly excluded, not passed;
macOS browser availability is recorded separately. #603 remains open pending its
remaining physical acceptance, and #597 stays open per the latest operator
instruction despite its completed Windows memory/tray acceptance.
