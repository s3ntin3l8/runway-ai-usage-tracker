# macOS Claude Code Keychain renewal: bounded main-account test

Date: 2026-10-21. Implementation PR: #751. Remaining acceptance: #750.
Repeated-prompt blocker: #764. #526 remains open and #751 remains draft.

## Authorization and readiness

The user explicitly approved a bounded test against their existing main Claude
Code login after discussing the possibility of needing to sign in again. They
confirmed that waiting permission prompts were allowed before continuation.
This supersedes the disposable-account prerequisite for this test only.

- macOS 26.6.2 (25G83), arm64; Claude Code 2.1.303.
- No Claude Code process was running before the test.
- No `CLAUDE_CONFIG_DIR` or secure-storage override was set.
- The local rebuilt macOS app passed ad-hoc signature verification.
- 150 targeted regression tests passed before live access.
- Initial read-only discovery found both tokens and a valid numeric expiry;
  the access token was expired. No credential values, provider identifiers,
  service suffixes or raw Keychain records are included in evidence.

## Native results

Two prechecks stopped before any exchange: one while a permission prompt was
waiting, and one when the acceptance harness treated `errSecNoAccessForItem`
(-25243) as failure. Apple's installed SDK defines that status as absence of a
legacy access descriptor, not denial. A metadata-only diagnosis identified it;
the harness was corrected without modifying the stored item or access settings.

The final bounded attempt succeeded:

- **One OAuth refresh and one credential write-back in total.**
- Intent and replacement journals were verified in Keychain; read-back verified
  the update, and the recovery record was removed.
- The same item reference, service and local account were retained. Access and
  refresh tokens changed, expiry advanced into the future, and unrelated JSON
  was preserved.
- The legacy access descriptor was absent before and after; available protected
  attributes were unchanged, and no opaque access-control object was reported.
  This comparison does not claim to inspect undocumented security constraints.
- Profile API: **401 before, 200 after**. Usage API: **200 after**, valid shape.
- `claude auth status --json`: exit **0**, signed in; its account matched the
  verified post-renewal profile. Original upstream identity continuity remains
  unverified because the expired original token returned 401.
- No model request was made. Background renewal was not enabled by this test.

## Prompt blocker and verification boundaries

The user reported approximately **15+ permission prompts**. That is unacceptable
for routine background renewal and is tracked in #764. The per-operation helper
process lifecycle is a likely contributor; this run did not establish the exact
cause or attribute each prompt to a particular operation. No further native
requests were started by the completed acceptance helpers. Later investigation
and the implementation's test-isolation incident are recorded in
[macos-keychain-prompt-repair-764.md](macos-keychain-prompt-repair-764.md).

Native IO ran through the production app's packaged helper; the supervising
renewal state machine came from matching source modules. An end-to-end packaged
daemon cycle and physical restart/denial/write-failure tests were not executed.
Synthetic tests cover recovery and denial/timeout behavior but do not replace
those native observations. Exact prompt counts and the user's individual button
choices were not recorded. Existing user-approved OS permission changes are not
attributed to the token write-back code.

The sanitized machine-readable result is
[claude-keychain-renewal-native-526.json](claude-keychain-renewal-native-526.json).
Keep #751 draft and #526/#750 open until the prompt blocker and remaining
acceptance are resolved. Another native test requires agreement on its scope;
no additional refresh is authorized by this completed one-exchange run.
