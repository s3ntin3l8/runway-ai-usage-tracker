# Packaged tray verification (#548, #526)

Date: 2026-10-21. PR #751. Built from `b0639e65` plus the tray lifecycle
changes in this PR; `dist/548/Runway Sidecar.app`, ad-hoc signature verified.
The user requested all necessary bounded verification on their existing main
Claude Code login and confirmed **Always Allow** for Runway Sidecar.

## Tray lifecycle

The tray now arms the existing keep-alive controller when its paired daemon
starts, replaces local permissions on config reload, disarms before transport
shutdown, and reopens the transport before starting renewers after a restart.
Unpaired instances remain unarmed. Fleet can activate Claude keep-alive, but
cannot grant local `claude_keychain_renewal` permission. Removal of that local
setting takes effect when config reloads.

## Native evidence

Tests launched the actual packaged app with an isolated `RUNWAY_CONFIG_DIR`
and a loopback acceptance server. No production server received test data;
production configuration was unchanged and installed Runway stayed stopped.
Only the Claude renewer was enabled; no file targets existed. Other providers,
Desktop OAuth, model requests and self-update were excluded from this test.

1. A fresh packaged helper read succeeded after foreground authorization.
   The actual tray received Fleet instructions and reported Claude active,
   logged **not due**, uploaded no credentials/metrics/events, and preserved
   credential bytes, access policy and recovery absence.
2. A preliminary controlled expiry-only write produced a differing access-policy
   comparison. The harness stopped before any exchange and restored the original
   expiry. The differing entry was not retained; this remains unexplained.
3. A separately instrumented run prepared the saved expiry within the normal
   15-minute lead time, without changing either token. Its fresh policy comparison
   passed. The actual packaged tray then completed one immediate renewal cycle
   and logged **renewed** once after verified write-back. It stopped before the
   60-second second tick. One exact target and no file targets bounded exchange
   count; no OAuth traffic interception was used.
4. Access and refresh credentials changed, expiry advanced beyond its original
   value, and exact item/service/account and unrelated JSON were preserved.
   Recovery was absent after completion. Fresh legacy metadata snapshots retained
   duplicate ACL entries and normalized authorization/application ordering. The
   access-policy and full integrity comparisons both passed for this write.
   Profile and usage returned 200, with the same profile before and after.
5. The first separate Claude Code status check reported not logged in and the
   user identified a `security` consent dialog. Runway helper restart/read
   consistency and API acceptance still passed. This is a separate authorization
   boundary; no refresh was repeated in response to the CLI failure.
6. An explicitly announced foreground `security` read succeeded. The subsequent
   read-only CLI check returned success, signed in and matched the verified
   account. Runway then started another fresh packaged helper and read identical
   credentials. Recovery remained absent. No additional exchange or write occurred.

Tokens, recovery payloads, profiles and raw ACL snapshots remained in memory,
private pipes or Keychain. Evidence contains only booleans, counts and statuses.
The earlier comparison failure remains part of the evidence; later success does
not retroactively explain it or prove stable consent across app updates.

## Remaining boundaries

#773 tracks the unresolved first-write policy delta. #750 retains native configured/suffixed service and physical fault recovery
acceptance, plus investigation of the earlier policy difference. #767 retains
stable signing/update consent. #764 retains broader consent-experience acceptance; this run verified Runway
and the owning CLI after their separate explicit foreground authorizations,
without measuring an exact dialog count. #548's Windows xAI live
write-back prerequisite is unexecuted. Keep #526 open while required acceptance
remains incomplete.

Runway locking excludes its own renewers. Keychain has no atomic refresh-token
comparison against an independently refreshing Claude Code process. Recovery
journaling cannot close the interval between provider rotation and saving the
response.

Machine-readable evidence: [claude-keychain-tray-acceptance-548.json](claude-keychain-tray-acceptance-548.json).
