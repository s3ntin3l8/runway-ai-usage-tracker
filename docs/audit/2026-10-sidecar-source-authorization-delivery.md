# Source authorization — delivery plan and findings

> **Snapshot — 2026-10-22, tracker #780. Not maintained.**
> This is the delivery half of the approved plan whose durable reference is
> [`../source-consent.md`](../source-consent.md). It records delivery order,
> open acceptance gates and the findings as they stood on that date.
> Current behaviour: [`../source-consent.md`](../source-consent.md).

## Native safety and renewal acceptance (#773 / #767 / #781 / #750 / #526)

Do not run native probes on the everyday Mac. Synthetic native tests run only in
disposable hosted macOS CI, with no provider credentials or release secrets,
process-group/job deadlines and isolation/cleanup checks. Incomplete experiments
fail. The latest metadata-only public update control timed out and reported lock
metadata changes; causality of those changes and recurring credential prompts is
unresolved. See
[claude-keychain-public-update-probe-773.md](../evidence/acceptance/claude-keychain-public-update-probe-773.md).
Subsequent #886
controls preserved observed isolation and demonstrated a creator/helper read
boundary, not a supported shared-access store. The current
[source renewal readiness inventory](2026-10-sidecar-source-renewal-readiness-781.md)
separates
implemented mechanisms, historical evidence and remaining acceptance gates.

Retain the unsupported-policy write guard until a public supported write path
proves policy preservation. Prefer an independently authorized Runway grant or a
proved owner-cooperation/shared-store mechanism. A copied Claude Code refresh token
is still shared ownership. Borrowed refresh tokens and recovery records stay
local; retain server-side prevention of CLI token rotation.

Exact-item identity, unrelated JSON fields, account/service/access policy, per-user
locking and pre-exchange/pre-write re-reads must be preserved. Recovery persists
verified intent before exchange and replacement before target write; pending
replacements never cause another exchange. Verify target read-back before cleanup,
never recreate deleted credentials, preserve independently newer logins and block
ambiguous interrupted refresh reuse. Keychain has no atomic comparison against an
independent Claude Code refresh; the provider-response-to-journal crash gap remains.

Developer ID signing is not available yet. It is an external gate for the intended
release identity and signed A-to-B update/rollback acceptance, not for synthetic
development. Stable signing alone does not prove access survives owner-side
credential replacement. Real-account acceptance requires a readiness report and
explicit credential-access/rotation approval in an isolated environment. No new
Anthropic account is required. Keep #526 and native acceptance trackers open until
their actual criteria pass. Verified intermediate milestones may ship honestly.

## Cloud usage and delivery (#669)

Revalidate the previously investigated Claude session/history interface before
implementing server-side cloud collection. Exhaust pagination, bound backfill,
checkpoint after event commit and reconcile assistant records versus cumulative
counters, resets, resumes, subagents and delayed updates. Deduplicate local/cloud
overlap and distinguish execution environment, sender and viewing activity.
Parse responses in memory and persist only usage fields, without transcripts.
Keep local fallback until coverage/accounting parity is demonstrated; do not claim
all Chat/Desktop/Cowork/mobile history is covered. Server collection continues
while credentials remain usable; borrowed CLI refresh is never moved server-side.

The [offline accounting audit](../evidence/acceptance/claude-cloud-accounting-669.md)
records SDK usage
observations and the live-verification gates; it does not enable cloud collection.

## Delivery order

#773 safety containment; #782 local consent; #783/#780 Fleet
lifecycle and UI; #767/#781 release identity and supported renewal; #669 cloud
collection. Run make lint, make test, make web-lint and make web-test for each PR,
self-review the diff, preserve coverage/security gates and request no additional
review while the user's instruction remains. Track every blocked/deferred gate in
linked GitHub issues. Green tests or safe refusal do not prove native acceptance.
