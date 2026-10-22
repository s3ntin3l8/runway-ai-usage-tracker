# Native Keychain test isolation repair

Tracks #526, #750, #764 and #773; implementation is in PR #751.

The former synthetic harness changed the user's default Keychain and search list.
Its locked-target fault used the child process's default Keychain, which could be
login rather than the temporary fixture. This was an unsafe test isolation failure.
The user reported repeated security login-Keychain unlock dialogs after Cancel.
They were gone after sleep and reopening the Mac; that observation does not prove
that terminating processes resolved the prompt loop or identify every requester.

## Repaired boundary

- No default-Keychain or search-list setters and no Keychain locking in the harness.
- A private directory contains freshly created synthetic Keychains and fake tokens.
  Fixture passwords remain in memory, never command arguments or logs.
- Both source and packaged workers use production native IO. A private transport
  restriction supplies explicit Keychain files for every query, rather than
  replacing the native API with a test implementation.
- Native queries carry an explicit search list and destination Keychain. Before
  accessing data through a persistent reference, metadata resolves the actual
  owning Keychain and rejects references outside the scope. Discovery matches
  receive the same ownership check. Open handles are released on every exit.
- The harness rejects missing paths, symlinks, paths outside its fixture,
  unexpected filenames and directories not privately owned with mode 0700 before
  framework loading. The native restriction additionally requires existing,
  absolute, owned regular files without multiple hard links.
- Fresh processes compare the real default, search list and lock state before and
  after every case. Cleanup deletes only the explicitly created fixture Keychains.
- No provider request is made. Native helpers disable interaction and bound calls.
  The timeout fault suspends only the test's own idle helper PID; it never locks a
  Keychain or stops a system process. The helper is then killed and reaped.

## Exact-package verification on 2026-10-22

Package built from source commit d3726328b7f9743cadbb107a38dd373e0acd6adf, with ad-hoc signature
verification passing. The full repository gate passed before committing the source:
make lint, make test (5,458 passed; 63 skipped), and make web-test (1,222 passed).
Commit hooks and a self-review passed. No additional review was requested.

A later CodeQL review of the harness exposed swallowed process interruptions. Two
non-native regression tests reproduced it; the harness now propagates Ctrl-C/exit
requests after cleanup and records an interrupted outcome. The command source is
assembled outside its argument list to remove concatenation ambiguity. The full
gate passed again (5,460 Python; 63 skipped; 1,222 frontend), and all three native
renewal matrices passed again against the same unchanged packaged worker.

| Run | Cases | Native IO | Target protection and scope |
| --- | ---: | --- | --- |
| Source renewal and source worker | 16 | Production source | Protected private Keychains; actual default/suffixed Claude service names |
| Source renewal and packaged worker | 16 | Actual packaged helper | Legacy private Keychains; actual default/suffixed Claude service names |
| Source renewal and packaged worker | 15 | Actual packaged helper | Protected packaged-owned items under Runway's recovery service; service discovery is substituted only in the test |
| Packaged worker boundary checks | 4 | Actual packaged helper | Protected private Keychains; foreign policy/reference, denial and timeout |

The 16-case matrices cover normal and suffixed service selection; failed intent
persistence; interrupted exchange with no saved response; saved replacement before
crash; successful target write before cleanup; interrupted cleanup; failed pending
persistence; failed target write; ambiguous response; independent rotation;
deletion; permission disabled while replacement is pending; malformed expiry;
missing expiry; and ambiguous item selection. The protected packaged-owned
15-case run omits suffixed Claude service derivation, which the other matrices cover.

Every matrix case preserved user Keychain metadata, deleted its temporary
Keychains, preserved unselected items and performed at most one fake exchange.
Pending replacements never triggered a second exchange after restart. Protected
fixtures included partition and integrity ACL entries. Successful writes preserved
the canonical access policy. Diagnostics were asserted secret-free and deduplicated.

The four packaged boundary checks separately established:

1. A foreign protected partition is rejected before credential-data mutation;
   original data and policy remain unchanged.
2. Actual native read denial retires the helper and suppresses subsequent discovery
   and renewal after transport reopening, with only one native dispatch.
3. A timed-out suspended helper is killed and reaped, and subsequent operations
   are suppressed without another helper start.
4. An out-of-scope persistent reference is rejected before copy/update/delete/
   preflight can access the foreign item. Its data remains unchanged.

Sanitized full results and a separate metadata-only real-item preflight are in
[claude-keychain-isolation-773.json](claude-keychain-isolation-773.json).

## Remaining real-login acceptance boundary

The exact new packaged worker found the user's unique existing Claude item, but
its metadata-only write preflight returned unsupported. This check read no token
data, wrote no credentials and made no exchange. Safe refusal is not successful
renewal. The latest build has not passed actual real-login refresh/write-back,
expiry advancement, policy preservation, Claude Code coexistence or a full
packaged-tray renewal. Historical actual packaged evidence predates the guard.

The protected packaged-owned matrix verifies actual native recovery IO and policy
preservation for the writer's own partition. It does not claim a packaged write
into a foreign protected Claude partition. Provider responses in all matrices are
synthetic; source renewal orchestration is not full compiled-tray orchestration.
Keep #526/#750/#773 open and do not call PR #751 acceptance-complete.

## Partition policy and one-time consent

The historical probe in claude-keychain-policy-probe-773.json changed only
partition authorization 65538 on its first data update. Its cached cleanup checks
came from the unsafe older harness and are not evidence of isolation.

Apple's classic data update regenerates the partition ACL for the writer. The
production preflight refuses foreign/multiple/unknown partitions before exchange,
repeats before exchange/write, and preserves any known pending replacement.
Legacy items without partition/integrity protection and protected items with
exactly the writer's partition remain eligible. No production ACL/partition setter,
owner-authorization repair or password-retention mechanism is added.

A stable identity and Always Allow help reading but do not preserve a second
application's partition across data writes. Item-owner authorization alone cannot
restore the reserved partition tag. The only successful private-fixture restore
prototype used a database password and a private Apple API; it is disabled and is
not a shippable authorization flow. See the supported-API evidence and unresolved
architecture in
[claude-keychain-one-time-authorization-773.md](claude-keychain-one-time-authorization-773.md).

Runway process locking and token re-reads cannot exclude independently running
Claude Code. Keychain provides no atomic refresh-token comparison. Journaling
cannot close the interval between provider rotation and durable response saving.
Stable signing/update consent remains separately tracked in #767.

Apple sources: [private-Keychain creation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/StorageManager.cpp)
and [classic data update](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/Item.cpp).
