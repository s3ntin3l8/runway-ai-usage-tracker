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
  Passwords remain in memory and are never passed to a process or logged.
- Source native workers open only the explicit fixture paths. Each item query has
  an explicit search list and destination Keychain. Before accessing data through a
  persistent reference, metadata resolves its actual owning Keychain and rejects
  references outside the fixture. Discovery matches get the same ownership check.
- Missing paths, symlinks, paths outside the fixture, unexpected filenames, and
  directories not privately owned with mode 0700 fail before framework loading.
- Fresh processes compare the real default, search list and lock state before and
  after every case. Cleanup deletes only the explicitly created fixture Keychains.
- No provider request is made. Native helper operations are noninteractive and
  bounded. The former locking fault and experimental owner-authorization probes
  remain removed or disabled.

## Verification on 2026-10-22

Eight safety tests pass without native IO, including a foreign login-Keychain
reference rejected before any credential read, update, delete or write preflight.
The explicitly scoped source-native matrix passed all 16 cases:
normal and suffixed services; failed intent persistence; interrupted exchange with
no saved response; saved replacement before crash; successful target write before
cleanup; interrupted cleanup; failed pending persistence; failed target write;
ambiguous response; independent rotation; deletion; permission disabled while a
replacement is pending; malformed expiry; missing expiry; ambiguous item match.

Every case preserved user Keychain metadata, deleted its temporary Keychains,
preserved unselected items, and performed at most one fake exchange. Pending
replacements never triggered a second exchange after restart. Protected fixture
Keychains included partition and integrity ACL entries. Successful writes preserved
the access policy. Sanitized evidence is in claude-keychain-isolation-773.json.

## Remaining acceptance boundary

The harness refuses packaged native IO before creating a fixture because the
packaged worker does not yet expose an equivalent test-only scoping boundary.
These source results must not be reported as packaged acceptance. Earlier real
packaged evidence predates the new #773 preflight guard. #750 remains the native
acceptance tracker; #526 must remain open until its full acceptance passes.

## Partition-policy guard

The historical synthetic probe in claude-keychain-policy-probe-773.json changed
only authorization 65538 on its first data update: a fresh writer could then read
the item it previously could not read. Its cached cleanup checks came from the
unsafe older harness and are not evidence of isolation.

Apple's classic Keychain data-update implementation regenerates the partition ACL
for the writer. A protected item owned by another application or by multiple
partitions can therefore change policy even on a data-only update. The new
preflight refuses such an update before exchanging a refresh token, repeats the
check before exchange and before write, and retains any known pending replacement.
It never attempts an owner-authorization or access-control repair. Legacy items
without partition/integrity protection and protected items with exactly the
current writer's partition remain eligible. If Claude Code changes that policy,
automatic renewal skips the item and reports that owning-app renewal is required.

Runway process locking and token re-reads do not exclude independently running
Claude Code. The Keychain API provides no atomic token comparison; that race
limitation remains.

Full repository gate passed: make lint, make test (5,441 passed; 63 skipped),
and make web-test (1,222 passed). No additional review was requested.

Apple source references: [StorageManager private-Keychain creation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/StorageManager.cpp)
and [classic Keychain data update](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_keychain/lib/Item.cpp).
