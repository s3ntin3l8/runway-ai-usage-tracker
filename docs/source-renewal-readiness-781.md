# #781: source authorization and renewal readiness

Audited on 2026-10-24 from repository baseline
[eb89056b](https://github.com/s3ntin3l8/runway-ai-usage-tracker/commit/eb89056b1a5dda4e44052695f8e6270accdf970f).
This inventory records implementation and evidence, not a new permission grant or
an acceptance pass. No credential files, browser profiles or native stores were
read; no owner CLI, provider request, login or model call was run for this audit.

## Terms and reporting

- **Collection:** a configured collector can try eligible access credentials. A
  parser or decryptor does not prove endpoint acceptance, account mapping or scope.
- **Runway renewal:** Runway exchanges a refresh token and saves the replacement
  into the exact owning store. A second copy is not an independent grant.
- **Owner cooperation:** Runway invokes a supported owner operation; the owner
  remains responsible for rotation and storage. This still requires quiet,
  bounded execution and proof that later reads and owner-side rotations work.
- **Acceptance:** evidence for the exact package, target, platform and grant. A
  historical successful file refresh does not qualify a different Keychain item,
  signed update, provider scope or owning application.

The local Sources report and Fleet protocol currently report
`renewal_supported=false`. The catalog's Claude `renewal=true` means that a
separate renewal permission can be requested; it is not support/acceptance proof.
Admin Fleet source status also exposes `renewal_capable = descriptor.renewal`,
which is true for the Claude Code descriptor. This is catalog capability metadata,
not a local permission grant or the runtime `renewal_supported` result. The existing
xAI (Grok), Antigravity (agy), Claude Code and Codex (ChatGPT) keep-alive mechanisms
are a different capability from a source's direct Keychain write support. Do not
infer one from the other.
See [source reporting](../scripts/sidecar_pkg/source_settings.py),
[Fleet reporting](../scripts/sidecar_pkg/source_protocol.py),
[admin Fleet status](../app/api/endpoints/fleet_sources.py), and
[keep-alive ownership rules](../app/services/refresh_policy.py).

## Native source inventory

This table covers all 25 IDs in `control_catalog()` at the audited baseline.
`{chrome,chromium,edge}` expands to three distinct source IDs with separate
consent. Windows lifecycle sources are included here but are not part of the
local macOS Keychain consent catalog. The inventory describes fixed code paths;
no discovered accounts or installation-specific identifiers are included.

| Source IDs | Read/collection path | Renewal mechanism and evidence boundary |
| --- | --- | --- |
| `anthropic.claude-code` | Exact Claude Code Keychain login; access material and expiry may feed subscription collection after local consent and eligible forwarding. | Guarded Runway write-back exists. Current foreign/shared policy has no proved preserving write path. #773/#750/#526 remain open; local read consent, literal-true renewal permission and effective keep-alive cannot bypass this guard. |
| `anthropic.desktop-oauth` | Claude Desktop OAuth decrypted using its Safe Storage resource; kept separate from Code and browser sessions. | Read-only. No Desktop refresh or write-back acceptance. API/scope/account eligibility must be verified separately from decryption. |
| `anthropic.desktop-session` | Claude Desktop cookie session, using its Safe Storage dependency. | Read-only; not the Desktop OAuth grant or the Claude Code login. No cookie-renewal mechanism. |
| `anthropic.{chrome,chromium,edge}` | Browser session cookies for Claude subscription collection. | Read-only extraction. Safe Storage grants decryption access, not an independent provider authorization or renewal capability. |
| `anthropic_console.{chrome,chromium,edge}` | Console session cookies for Console collection. | Read-only extraction; Console billing/authentication is not proof of subscription quota scope. |
| `chatgpt.{chrome,chromium,edge}` | ChatGPT browser sessions. | Read-only extraction; the separate Codex file renewer does not renew these cookies. |
| `kimi_coding.{chrome,chromium,edge}` | Kimi browser sessions. | Read-only extraction; no source-specific cookie renewal. |
| `opencode.{chrome,chromium,edge}` | OpenCode browser sessions. | Read-only extraction; xAI file/database write-back is a separate source and grant. |
| `ollama.{chrome,chromium,edge}` | Ollama Cloud browser sessions. | Read-only extraction; no source-specific cookie renewal. |
| `github.gh-cli` | GitHub CLI-managed credential lookup. | Owner-managed credential; no Runway token exchange or native-store write-back implemented for this source. CLI execution does not itself prove permission/update continuity. |
| `github.windows-credential` | Existing Windows Credential Manager collection. | Read-only native collector/lifecycle source; macOS consent does not cover it. No direct renewal acceptance recorded here. |
| `antigravity.agy-cli` | Antigravity-owned macOS Keychain login. | Local keep-alive can invoke `agy models` after expiry, gated by local consent and effective keep-alive. This is owner cooperation, not Runway editing the item. Current packaged owner-rotation/quiet-access and signed continuity still need evidence. |
| `antigravity.windows-credential` | Antigravity-owned Windows Credential Manager login. | Owner-command keep-alive exists; a macOS result is not Windows proof. No independent Runway grant established by this inventory. |

The source of truth is the static [catalog](../scripts/sidecar_pkg/source_catalog.py)
and generated rules. Shared Safe Storage access never opts another scraper in.
Disabling a scraper, a remote pause or a denial must retain their existing local
operation/forwarding boundaries; this document changes none of them.

## File, owner-command and observation paths

The file and observation paths below are outside the new per-scraper Keychain
consent boundary. Antigravity owner-command cooperation retains its platform's
credential-access rules: on macOS, `antigravity.agy-cli` local consent is required
before the Keychain read and keep-alive invocation. Without it, keep-alive stays
quiet. Existing activation and source-lifecycle rules remain in force for every
path.

| Source family | Implemented mechanism | Existing evidence and remaining gate |
| --- | --- | --- |
| Claude Code credentials file | `AnthropicRenewer` refreshes and atomically writes the owning file under the Claude renewer lock; unknown JSON fields survive. | [Historical probe](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/576#issuecomment-6038701847) and [later live-file observation](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/576#issuecomment-6066080600) support the existing route. Retain that verified file route; it is not an independent grant and cannot establish foreign-Keychain policy preservation. |
| Codex credentials file | `CodexRenewer` exchanges and atomically updates the owning `auth.json` fields. | The [historical disposable-login probe](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/524#issuecomment-6048061387) supports this route. It does not qualify ChatGPT browser sessions or establish that an OAuth client belongs to Runway. Provider reuse windows are not concurrency guarantees. |
| xAI Grok/OpenCode credentials | `XaiRenewer` saves to the selected owner JSON or OpenCode database credential; unrelated records remain. | Existing implementation/tests and the distinct live/platform gates in #548 must be assessed together. A storage test does not prove endpoint acceptance or owning-CLI coexistence. |
| Antigravity file/native login | `KeepAliveThread` invokes the resolved owner CLI's `models` command when due. | The module records historical owner-command verification. Current packaged, platform-specific quiet reads and owner-driven rotation are still separate gates; the command was not run in this audit. |
| MiniMax Code credentials | Owner rotates its own stored token generation; Runway discovers it read-only. | No Runway keep-alive/write-back route. Machine-owned server refresh remains prevented. |
| Claude status-line observations | The existing `file_json_statusline` rule reads recently written quota observations. | No credential renewal. Provider docs describe quota fields only after the session's first API response, with independently optional windows. This cannot establish offline or inactive-session freshness. |
| Other file/environment/Safari/local usage inputs | Existing scraping/collection paths. | Do not add Keychain opt-ins or a generic renewal claim to these paths. A source containing a token is not evidence of an independently authorized renewable grant. |

Relevant implementations: [Claude file renewal](../scripts/sidecar_pkg/anthropic_renewer.py),
[Codex renewal](../scripts/sidecar_pkg/codex_renewer.py),
[xAI renewal](../scripts/sidecar_pkg/xai_renewer.py), and
[owner-command keep-alive](../scripts/sidecar_pkg/keep_alive.py).
Historical verification claims above come from existing repository evidence; no
live acceptance was repeated or upgraded by this documentation audit.

The server's rotating-provider guard remains in force for machine-owned Claude,
Codex, xAI and MiniMax credentials. Existing eligible Google refresh policy is
separate; a nonrotating-token implementation assumption is not proof of a
Runway-owned grant. Borrowed Claude refresh tokens and recovery records stay local.
Server collectors may keep using eligible access credentials while a Mac is
offline; there are no heartbeat credential leases.

## Claude decision and prerequisites for the next experiment

The [#886 evidence](claude-keychain-helper-authorization-773.md#hosted-results-2026-10-24)
established a fresh-process helper read of its own item, followed by denial of the
separately trusted Python reader. Ordinary trust entries did not establish shared
protected-data access. The selected partition identity tracked the creator. The
Python-created helper trust-blob mismatch is an additional representation unknown;
these observations do not establish the historical timeout/prompt-loop cause.
Another write matrix cannot resolve the missing authorization design by itself.

The current primary provider documentation establishes these boundaries:

- The [CLI reference](https://code.claude.com/docs/en/cli-reference) lists login,
  logout and status; no refresh-only operation was found. An authentication status
  result does not prove rotation. Starting a model session to force refresh is
  outside discovery/acceptance authorization.
- [Subscription token setup](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
  produces a token documented for model requests. It does not establish quota
  access or an independently registered Runway client.
- [Platform authentication](https://platform.claude.com/docs/en/manage-claude/authentication)
  describes API keys, Workload Identity Federation and Messages-only App Attest.
  These docs do not establish the required subscription quota grant.
- [Status-line quota fields](https://code.claude.com/docs/en/statusline#available-data)
  provide owner-produced observations for supported sessions. They do not transfer
  a renewable authorization or satisfy server freshness while the owner is idle.

Absence from the reviewed documentation is an unresolved support question, not
proof that every possible provider-supported mechanism is impossible. Before any
new native mutation experiment, produce a separately reviewed design with one of:

1. **Independent grant:** provider-supported Runway client registration, user
   consent, the required usage/profile scope, and a distinct refresh lineage.
2. **Owner cooperation:** a documented bounded non-model operation through which
   Claude Code performs its own renewal, with exact-account selection, safe exit
   behavior, quiet background operation and subsequent access for both apps.
3. **Shared store:** a public supported mechanism demonstrated to preserve the
   exact existing item's complete access policy and both apps' access through
   writes and owner-side rotation. Moving/copying the credential is not this proof.

No candidate has passed that gate for the current Claude Keychain requirement.
Do not change the production guard, borrow another app's identity, alter partition
policy, retain the login-Keychain password, or replace the existing Claude item.
Developer ID signing is still an external prerequisite for normal signed-release
continuity; it does not alone establish foreign-partition write preservation.

Once a supported design exists, the sequence is: review its scoped synthetic
experiment; run it only in disposable hosted macOS CI with the existing deadlines
and isolation checks; prove current packaged behavior; publish a real-acceptance
readiness report; obtain explicit access/rotation approval in an isolated
environment; verify one actual refresh, expiry advancement, complete policy and
JSON preservation, recovery cleanup, separate Claude Code acceptance and owner-side
rotation. Signed immutable A-to-B update/rollback remains #767. Runway locking
cannot exclude an independent owner process atomically, and durable journaling
cannot eliminate the provider-response-to-record crash interval.

## Delivery status and next action

This inventory advances #781's evidence audit; it does not close #781, #773, #750
or #526. Existing verified file renewal stays available, read-only native sources
stay distinct, and current source reporting remains conservative. #669 cloud
collection retains its separate pagination/accounting/provenance gates.

The next prerequisite for Claude renewal is provider-supported authorization or
owner cooperation evidence, not another real-account attempt. A concrete request
for that evidence should ask whether a third-party app can obtain the required
quota/profile grant, whether the owner exposes a non-model renewal operation, and
what policy/access guarantees survive owner rotation. This document prepares
those questions; no message has been sent to the provider.
