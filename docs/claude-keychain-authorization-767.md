# Claude Code Keychain authorization (#767)

The background sidecar never opens Keychain consent dialogs. Existing Claude Code
items can require both a trusted-application ACL match and an independent
partition-list match. Adding the app in Keychain Access alone may not satisfy both.
Apple describes these as independent conditions in its
[file-Keychain access-control guidance](https://developer.apple.com/forums/thread/845980).

To explicitly request access, run the installed app's own executable in a terminal:

```sh
"/Applications/Runway Sidecar.app/Contents/MacOS/RunwaySidecar" --authorize-claude-keychain
```

This standalone command selects one existing Claude Code item for the local
account and makes one foreground read with consent enabled. Default service and
environment-based Claude directory suffixes use the existing derivation. If
multiple services/items match, it stops before requesting consent. For a custom
directory, set the same `CLAUDE_CONFIG_DIR`/`CLAUDE_SECURESTORAGE_CONFIG_DIR` used by
Claude Code. No arbitrary service/account arguments are accepted.

Approve only the expected macOS request for the intended Runway app. The OS
controls authorization and may change its recorded permission when the user
approves. The command itself never edits ACLs or partition lists, unlocks a
Keychain, exchanges tokens, forwards credentials, or enables keep-alive/renewal.
It exits after that one read and restores the previous interaction policy. A
60-second watchdog bounds a stalled native call. Errors include only a sanitized
reason and, when available, the numeric OSStatus. Tokens and raw native errors
are never printed.

After authorization, restart the sidecar to clear its earlier access-denial
suppression and verify a fresh noninteractive read before enabling renewal. A
successful foreground read does not establish unattended access, API acceptance,
or renewal/write-back acceptance. Authorize the exact executable that will make
the background requests; granting access to another copy does not prove this copy
is authorized.

Current distribution builds are ad-hoc signed. Their code-hash-based designated
requirement changes with a rebuild. Stable signing and permission persistence
across updates remain tracked by #767; this command does not solve that release
identity problem. Do not add an identifier-only signature requirement or broaden
the credential to all applications as a workaround.

## Native investigation on 2026-10-21

The user explicitly authorized real-account verification instead of a disposable
Anthropic account. Two bounded read-only attempts stopped on denial. A third,
after the user added the exact test bundle using Keychain Access, also stopped
on denial. Each attempted one exact lookup plus its reference read, with no
refresh, credential write or retry. No new SecurityAgent process was observed
during those reads; that monitor is a UI-activity proxy, not an exact dialog count.

Metadata-only inspection verified the item was in an unlocked Keychain, retained
the existing trusted application and added the correct test app. Its independent
partition ACL contained one code-hash entry matching neither the test app nor the
installed app. No secret was requested during metadata inspection. That evidence
explains why trusted-app enrollment alone was insufficient; it does not establish
all possible conditions on the credential.

One synthetic setup attempt used `SecKeychainItemSetAccess` on a disposable item.
It unexpectedly opened a password-gated authorization request despite interaction
being disabled. The user cancelled; the call returned -128. Cleanup verified the
original default/search list and deletion of the temporary Keychain. No real
item was selected. A second fixture's creation-time partition policy did not
produce the expected denial, and a synthetic-only no-UI password edit returned
-25293. Neither attempt establishes a passing native partition-repair test. No
such edit was attempted on a real item; private ACL-edit APIs are absent from
the product implementation.

Correction to the earlier #526 evidence: the prior absence result for a legacy
access descriptor came from a different reference path. A metadata-only legacy
item lookup successfully returned its ACL. The earlier absence result must not
be treated as proof that the real item had no access-control policy.

Full packaged-daemon renewal, expiry advancement, real-item security metadata
comparison and Claude acceptance remain pending until recorded native results
establish them. Keep #526/#750/#764/#767 open.
