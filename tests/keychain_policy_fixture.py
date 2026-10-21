"""In-memory comparison for native acceptance; reports contain no ACL metadata."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PolicySnapshot:
    """Retain duplicate entries without exposing application data in repr."""

    entries: tuple[tuple[Any, ...], ...] = field(repr=False)


def canonicalize(entries: list[dict[str, Any]]) -> PolicySnapshot:
    """Normalize unordered authorizations and trusted applications."""
    result = []
    for entry in entries:
        applications = entry["applications"]
        result.append(
            (
                tuple(sorted(entry["tags"])),
                tuple(entry["selector"]),
                None if applications is None else tuple(sorted(applications)),
                entry["description"],
            )
        )
    return PolicySnapshot(tuple(result))


def compare(before: PolicySnapshot, after: PolicySnapshot) -> dict[str, Any]:
    """Report integrity-only changes separately from access authorizations."""
    old, new = Counter(before.entries), Counter(after.entries)
    changed = (old - new) + (new - old)
    policy_tags = sorted({entry[0] for entry in changed if entry[0] != (65539,)})
    return {
        "access_policy_preserved": not policy_tags,
        "integrity_changed": any(entry[0] == (65539,) for entry in changed),
        "changed_policy_authorizations": [list(tags) for tags in policy_tags],
    }


def capture(service: str, account: str) -> PolicySnapshot:
    """Read metadata without UI, releasing legacy handles before any update.

    The caller must first establish unique selection with KeychainStore.find.
    No passwords are requested; callers retain snapshots only in memory.
    """
    import ctypes as c

    from scripts.sidecar_pkg.macos_keychain_store import _NativeAPI

    class Selector(c.Structure):
        _fields_ = [("version", c.c_uint16), ("flags", c.c_uint16)]

    api = _NativeAPI()
    p = c.c_void_p
    bindings = [
        (
            "SecKeychainFindGenericPassword",
            [p, c.c_uint32, c.c_char_p, c.c_uint32, c.c_char_p, p, p, c.POINTER(p)],
        ),
        ("SecKeychainItemCopyAccess", [p, c.POINTER(p)]),
        ("SecAccessCopyACLList", [p, c.POINTER(p)]),
        ("SecACLGetAuthorizations", [p, p, c.POINTER(c.c_uint32)]),
        ("SecACLCopyContents", [p, c.POINTER(p), c.POINTER(p), c.POINTER(Selector)]),
        ("SecTrustedApplicationCopyData", [p, c.POINTER(p)]),
    ]
    for name, args in bindings:
        api._bind(api.sec, name, args, c.c_int32)
    owned = []

    def checked(code: int) -> None:
        if code:
            raise RuntimeError("policy metadata unavailable")

    def output():
        ref = p()
        owned.append(ref)
        return ref

    entries = []
    try:
        with api._without_interaction():
            item, access, acls = output(), output(), output()
            svc, acct = service.encode(), account.encode()
            checked(
                api.sec.SecKeychainFindGenericPassword(
                    None, len(svc), svc, len(acct), acct, None, None, c.byref(item)
                )
            )
            checked(api.sec.SecKeychainItemCopyAccess(item, c.byref(access)))
            checked(api.sec.SecAccessCopyACLList(access, c.byref(acls)))
            for index in range(api.cf.CFArrayGetCount(acls)):
                acl = api.cf.CFArrayGetValueAtIndex(acls, index)
                tags, count = (c.c_int32 * 64)(), c.c_uint32(64)
                checked(api.sec.SecACLGetAuthorizations(acl, tags, c.byref(count)))
                if count.value > 64:
                    raise RuntimeError("policy metadata unavailable")
                apps, description, selector = output(), output(), Selector()
                checked(
                    api.sec.SecACLCopyContents(
                        acl, c.byref(apps), c.byref(description), c.byref(selector)
                    )
                )
                applications = None
                if apps:
                    applications = []
                    for app_index in range(api.cf.CFArrayGetCount(apps)):
                        app = api.cf.CFArrayGetValueAtIndex(apps, app_index)
                        data = output()
                        checked(api.sec.SecTrustedApplicationCopyData(app, c.byref(data)))
                        applications.append(api._decode(data))
                entries.append(
                    {
                        "tags": tuple(tags[: count.value]),
                        "selector": (selector.version, selector.flags),
                        "applications": applications,
                        "description": api._decode(description) if description else None,
                    }
                )
        return canonicalize(entries)
    finally:
        for ref in reversed(owned):
            if ref:
                api.cf.CFRelease(ref)
