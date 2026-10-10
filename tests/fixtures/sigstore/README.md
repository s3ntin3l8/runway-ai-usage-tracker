Public verification fixture from the repository's `edge` release built from
`1f35acfa13c47f38b2b9e644533a61ae1b9cd61a` on 2026-10-09.

`SHA256SUMS.txt.sigstore.json` was reconstructed from the published detached
signature/certificate and its public Rekor entry, then verified with the official
Sigstore Python client against the exact Runway build workflow and GitHub issuer.
`trusted_root.json` contains public production trust anchors obtained through the
client's authenticated TUF refresh. These files contain no private keys or secrets.

Tests use the real cryptographic verifier offline. The short-lived signing
certificate has expired, exercising verification at the authenticated signing
time rather than current-time certificate validity.
