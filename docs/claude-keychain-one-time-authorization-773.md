# One-time Claude item authorization investigation

Tracks #526, #750, #767 and #773; implementation PR #751.
Investigated on 2026-10-22. This is an unresolved acceptance constraint, not an
implemented enrollment flow or permission change.

The user approved developing explicit item-owner authorization, then asked for a
one-time Always Allow setup. They did not approve retaining the login-Keychain
password. No such retention or password UI has been implemented.

## Why a stable read grant does not solve write preservation

Classic Keychain application ACLs and application partitions are separate checks.
A stable signed identity helps preserve a read grant across launches and updates,
but it does not change the classic data-update algorithm. Apple's ItemImpl::update
passes nonempty replacement data to updateSSGroup. That creates a new secure-storage
group, copies ordinary access entries, and removes partition and integrity entries
before regenerating them for the writer. A previously authorized second partition
can therefore disappear even when the same writer and executable are reused.

The protected synthetic first-write probe reproduced a change to partition
well-known authorization 65538, not merely integrity authorization 65539. The
production guard refuses foreign/multiple/unknown partition policies before
spending a refresh token. Refusal protects the login but fails the intended
real-login renewal acceptance.

A metadata-only check on the user's existing item found one exact target but an
unsupported write policy. It made zero credential-data reads, writes or provider
exchanges. No expiry or permission change was made for this check.

## Owner consent is insufficient for reserved partition edits

Apple's SecurityServerAcl::changeAcl explicitly preserves the partition tag unless
the caller supplies credentials that independently protect the Keychain database.
ObjectAcl::cssmChangeAcl checks the owner and then separately rejects edits to a
preserved tag. Consequently, granting item-owner permission does not remove this
partition restriction. KeychainDatabase::checkCredentials ignores interactive
prompt samples; it accepts a valid explicit database password or database key.
An unlocked Keychain and an Always Allow read grant are not equivalent to those
credentials.

An isolated prototype used only the random password of a freshly created private
Keychain. It updated fake data with SecItemUpdate, copied the resulting fresh
access object and restored the saved partition using
SecKeychainItemSetAccessWithPassword. The complete canonical policy then matched;
no owner change was needed. However, that function is declared in Apple's
SecKeychainItemPriv.h, not the public header. This private-API experiment proves
why possession of the database credential matters; it is not an approved or
supported product implementation. The prototype is disabled after investigation. Sanitized experimental results are
in [claude-keychain-authorization-feasibility-773.json](claude-keychain-authorization-feasibility-773.json).
An attempted fabricated owner ACL was rejected by the OS and supplies no evidence
of a working owner-enrollment flow. Its disposable fixture was deleted, with fresh
metadata checks confirming user Keychain state was unchanged.

No public, persistent, item-only authorization for restoring arbitrary reserved
partitions has been established. Do not represent signing work alone as resolving
this issue, add a password store, grant all applications access, migrate/recreate
Claude's item, or silently use a system interpreter's identity to bypass the
application boundary. Those are different designs with different permissions.

## Public update alternatives checked

A follow-up source and installed-SDK check on 2026-10-22 examined the public
legacy update APIs and a combined data/access update. In the pinned Apple
implementation, `SecItemUpdate` reaches `SecKeychainItemModifyContent`, whose
`ItemImpl::modifyContent` stores replacement data and calls `update()`.
`SecKeychainItemModifyAttributesAndData` also reaches the same item update path.
Switching to a legacy public data setter therefore does not avoid secure-storage
group replacement.

Passing the saved `kSecAttrAccess` in the same `SecItemUpdate` call does not
install it atomically with the replacement data: `_UpdateKeychainItem` performs
the data update first and then calls `SecKeychainItemSetAccess`. The reserved
partition checks described above still apply to that second operation. A
failure there cannot be interpreted as proof that the data was not written.
This is source-derived evidence, not a newly executed native mutation.

The installed macOS SDK public `SecKeychainItem.h` declares
`SecKeychainItemSetAccess` and the legacy data setter, but does not declare
`SecKeychainItemSetAccessWithPassword`. No private password-based API or
low-level storage bypass has been added to the product. These checks narrow the
remaining investigation; they do not prove that every possible supported
authorization architecture is unavailable. No real credentials were read,
updated, or refreshed during this follow-up.

Source pointers at the same pinned Apple revision:

- [SecItem combined update order](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecItem.cpp#L2517-L2526).
- [Public legacy API entry points](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecKeychainItem.cpp#L130-L186).
- [Item data updates share secure-storage handling](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/Item.cpp#L819-L861).

## Delegating refresh to Claude Code

The current official [CLI reference](https://code.claude.com/docs/en/cli-reference)
lists login, logout, authentication status, and long-lived token setup. It does
not document a refresh-only subcommand for the existing subscription login.
Authentication status is not proof that a refresh occurred. No CLI/model session,
login flow or alternate-token setup was executed to force a refresh. Delegation
therefore remains an unproven redesign, not an acceptance workaround.

## Acceptance decision still required

The current implementation preserves policy by declining unsupported writes.
The original acceptance still requires an actual refresh of the existing login,
verified write-back, unchanged access policy and Claude Code coexistence. No
replacement criterion has been approved. #526 and #750 must stay open; PR #751
must not be described as acceptance-complete or merged on the strength of its
synthetic matrices. A supported architecture satisfying one-time consent and
cross-application policy preservation remains to be established.

Runway locking and re-reads cannot provide atomic exclusion against Claude Code.
Neither intent journaling nor authorization eliminates the provider-rotation to
durable-response crash interval.

Primary Apple source, inspected at Security revision
db15acbe6a7f257a859ad9a3bb86097bfe0679d9:

- [Data writes create a new secure-storage group](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/Item.cpp).
- [Reserved partition edits require database credentials](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/securityd/src/acls.cpp).
- [Owner validation and reserved-tag checks are separate](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_cdsa_utilities/lib/objectacl.cpp).
- [Database credential validation ignores prompt samples](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/securityd/src/kcdatabase.cpp).
- [Password-based setter is a private API](https://github.com/apple-oss-distributions/Security/blob/db15acbe6a7f257a859ad9a3bb86097bfe0679d9/OSX/libsecurity_keychain/lib/SecKeychainItemPriv.h).
