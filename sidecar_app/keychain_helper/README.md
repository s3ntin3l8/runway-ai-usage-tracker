# Stable Keychain reader

`bin/kc_helper` is the **committed, separately signed** universal macOS reader
(arm64 and x86_64, minimum macOS 11). Sidecar releases must reuse these exact
bytes. Rebuilding it, modifying its Mach-O load commands, or signing it again can
invalidate existing “Always Allow” grants. A source or toolchain change is an
intentional helper update requiring re-authorization and a reviewed golden hash.

The implementation uses C and Apple's CoreFoundation/Security frameworks, with
no Python or Swift runtime embedded in the executable. The small reader stays
independent of sidecar, dependency and PyInstaller updates.

## Build and sign deliberately

From the repository root on macOS:

```sh
sidecar_app/keychain_helper/build.sh
```

The script compiles each architecture with `-std=c11 -Os`, minimum macOS 11,
links CoreFoundation and Security, combines the slices with `lipo`, and signs
**only this executable**, with this exact command:

```sh
codesign --force --sign - --identifier com.runway.sidecar.keychain-helper --timestamp=none sidecar_app/keychain_helper/bin/kc_helper
```

Never use `--deep` on this executable or an enclosing bundle. Do not rebuild
this helper as part of an ordinary release. Bundle-signing and packaging
preservation are tracked separately in #1346; the current CI deep-signing step
still needs that change before release-to-release grants can be preserved.
PyInstaller must also preserve the committed helper's signature and bytes.

Inspect and verify each architecture:

```sh
codesign --verify --strict --all-architectures sidecar_app/keychain_helper/bin/kc_helper
codesign -dvvv --arch arm64 sidecar_app/keychain_helper/bin/kc_helper
codesign -dvvv --arch x86_64 sidecar_app/keychain_helper/bin/kc_helper
```

`kc_helper.cdhash` records `<architecture> <CDHash>`, one line per slice.
Every slice must have identifier `com.runway.sidecar.keychain-helper` and match
its golden CDHash. Keep this file and the committed executable together.
The golden hashes were built with Xcode 27.2 (27B52), Apple clang
21.0.0 (clang-2100.4.1.1), SDK 27.2. Other SDK/compiler versions may produce
different bytes; installing a new toolchain is not a reason to update the
committed binary or golden hashes.

On the macOS worker, three consecutive clean builds produced identical
universal SHA-256 `55c17ef439b61f2250f8bc6f8a3bc9ea1de1a867169750d46c6c454fee839e3e`
and identical per-slice CDHashes as recorded in the golden file. The linker's
normal content-derived UUID is retained: removing LC_UUID prevents execution
on current macOS. No build timestamp or random signing timestamp is embedded.

## Private protocol and boundary

The helper accepts the existing `keychain_protocol.py` framing: a four-byte
big-endian length followed by a binary plist, bounded by `MAX_FRAME` (4 MiB).
Requests have exactly `operation`, `query`, and `values`. Only the `copy`
operation is accepted; `values` must be empty. Unknown fields, selectors,
operations, and services return only `{"error": "unavailable"}`. Invalid framing
closes the process silently. EOF exits; incomplete requests, blocked writes and
native calls are bounded by `TIMEOUT` (30 seconds). An idle worker may remain
alive indefinitely. There are no error messages, native diagnostics or values
printed to a terminal or log. Results (including secret bytes and required item
identity) travel only in protocol frames over the parent's private stdout pipe.
The caller discards stderr; never run a real read with stdout connected to a
terminal or a log file.

The literal `SERVICES` table equals the source catalog's service set; tests
force deliberate updates when that catalog changes. The Claude source also
recognizes the existing CLI directory-derived service family:
`Claude Code-credentials-` followed by exactly eight lowercase hexadecimal
characters, as derived by `claude_code_keychain.service_candidates` and mapped
by `source_catalog.source_for_origin`. No general prefix matching is accepted.

Callers cannot inject native query dictionaries, access groups, Keychain paths,
classes or arbitrary accounts. Claude reads use the operating system login
account (not `$USER`); Gemini uses the catalog account `antigravity`. For the
other services the catalog has no account selector: the helper enumerates that
service, and the caller rejects ambiguity. Explicit account selectors for these
services are refused. Returned native attributes are reduced to `service`,
`account`, `persistent`, and, for a data read, `data`.

Searches return only item metadata. A persistent-reference read is permitted
only for a reference issued by a successful allowlisted search in **the same
helper process** (up to 4096 references). Metadata is checked again before the
data read and identity is verified afterwards. A reference from another process
cannot bypass service/account restrictions. All nonzero Security statuses are
refusals; item-not-found is `missing`, other statuses are `denied`.

Default execution disables interaction. The explicit local authorization flow
runs the same executable with `--interactive`; only the selected persistent
reference's data read may prompt. Select **Always Allow**, not **Allow**. The
flag does not expand the service/account allowlist or permit writes. Like the
OS grant itself, anyone able to execute this helper can use its allowlisted
reads; sidecar opt-ins are enforced by the caller, not an authentication claim
made by this helper.

Claude renewal mutations (`check_update`, `update`, `add`, `delete`) and reads
of their Runway recovery records remain on the Python worker, with separate
grants. They are not admitted to this read-only principal.

## Tests

```sh
.venv/bin/pytest tests/unit/test_keychain_helper.py tests/unit/test_keychain_helper_native.py tests/unit/test_keychain_helper_client.py
```

The real executable is exercised only with requests rejected before Keychain
lookup. A separately linked synthetic Security API exercises successful reads,
denials and identity races without reading or writing the user's Keychain.
The client tests substitute a fake executable for success, denial and timeout.
The binary frame fixtures contain synthetic values only.
