# Bounded real-login follow-up (#767)

Date: 2026-10-21. Code: `494530d6`, PR #751 (ready at the user's request).
The user authorized all work necessary for bounded verification on their existing
main Claude Code login. This follow-up performed **one additional OAuth exchange
and one credential replacement**, distinct from the earlier #526 native run.

## Authorization and read acceptance

The first explicit foreground command timed out. A later foreground command
succeeded after the user chose **Allow**, but a fresh background helper was still
denied. Another explicitly announced foreground read completed, after which a
new noninteractive packaged helper read the exact item and both profile and
usage returned 200. No exchange occurred during these authorization/read stages.

The credential was valid and not due. The normal not-due path was exercised
without an exchange. The bounded acceptance harness then temporarily adjusted
only the source supervisor's due threshold to permit an early refresh. It did
not change the stored expiry, local configuration, production process, or the
production 15-minute lead time. The exchange wrapper prohibited a second request.

## Renewal results

- One refresh and one exact-item write-back completed. Intent and replacement
  records were verified in Keychain, the target read back, and recovery removed.
- Access and refresh credentials changed; expiry advanced into the future.
  The exact item/service/local account and unrelated JSON were preserved.
- Profile returned 200 before and after, with the same upstream identity.
  Usage returned 200 with the expected shape after renewal.
- The initial combined check stopped during the remaining acceptance checks:
  its full ACL snapshot differed, Claude Code did not initially confirm a login,
  and the SecurityAgent process monitor observed new activity. It did not make
  another exchange.
- A separately bounded read-only follow-up passed: recovery absent, future
  expiry, a new helper after restart read identical saved credentials, profile
  200, and `claude auth status --json` returned 0, signed in, matching that profile.
  That follow-up made no refresh or credential write.

Tokens and recovery payloads remained in memory/private pipes/Keychain. Only
sanitized booleans, counts and statuses were saved. No model request was made.
All Runway/CLI test requesters exited; the installed sidecar remains stopped,
and its production configuration was not changed.

## Limits that remain open

**Access-policy preservation is not established by this run.** The real item's
full before/after ACL comparison returned false. Its raw snapshots were retained
only in the completed process, so this evidence cannot identify the differing
entry or retroactively prove it was just integrity metadata. A later synthetic
fixture (fresh legacy metadata reference on each read) verified that packaged
`SecItemUpdate` preserved every access-policy entry, with no snapshot change or
new SecurityAgent process, but did not reproduce the real difference. One earlier
fixture used a stale legacy reference after update and returned -25300; its
cleanup passed. All temporary Keychains were deleted and their original
search lists/defaults restored. Keep semantic real-item policy comparison open
under #750 rather than rotating the real token again to fill the evidence gap.

**Do not claim prompt-free native acceptance.** The user reported two additional
dialogs and chose Always Allow on both; their application names were not recalled.
A later dialog during the read-only Claude Code check named `security`. The
process monitor cannot attribute every dialog. No further CLI probe or automatic
consent retry was started after that check completed. #764 remains open.

**This is source-supervised renewal with packaged native IO.** The whole tray
daemon's renewal path was not executed. The tray deliberately leaves its
keep-alive controller unarmed; that existing feature gap is tracked by #548.
Configured/suffixed real-login acceptance and physical recovery fault injection
remain in #750. Stable signed identity and permission persistence across app
updates remain in #767. Keep #526 open until the required native acceptance passes.

The machine-readable evidence is
[claude-keychain-renewal-followup-767.json](claude-keychain-renewal-followup-767.json).

## Later tray verification

The later [packaged tray acceptance](claude-keychain-tray-acceptance-548.md)
implements #548 lifecycle wiring and verifies an actual packaged-daemon refresh,
canonical policy preservation in that run, and CLI/API acceptance. It supersedes
the unarmed-tray limitation above. The earlier snapshot discrepancy remains
unexplained under #773; #750/#767 retain other native acceptance boundaries.
