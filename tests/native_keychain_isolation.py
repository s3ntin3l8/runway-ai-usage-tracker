"""Test-only native scoping. Never installed in or imported by the sidecar."""

from __future__ import annotations

import ctypes as c
import os
import re
import stat
from pathlib import Path
from typing import Any


def validate_scope(root: Path, paths: list[Path]) -> tuple[Path, ...]:
    """Fail before loading frameworks when a supplied path escapes the private fixture."""

    def reject() -> None:
        raise ValueError("invalid native fixture scope")

    if (
        root.is_symlink()
        or not root.name.startswith("runway-native-526-")
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
        or not paths
    ):
        reject()
    resolved = root.resolve()
    for path in paths:
        if (
            path.is_symlink()
            or path.parent.resolve() != resolved
            or not re.fullmatch(r"fixture-[0-9]+\.keychain(?:-db)?", path.name)
            or not path.is_file()
            or path.stat().st_uid != os.getuid()
        ):
            reject()
    return tuple(path.resolve() for path in paths)


def scoped_api(root: Path, paths: list[Path]) -> Any:
    """Attach explicit search/add Keychains and check persistent-reference ownership."""
    allowed_paths = validate_scope(root, paths)
    from scripts.sidecar_pkg.macos_keychain_store import KeychainError, _NativeAPI

    class ScopedAPI(_NativeAPI):
        def __init__(self):
            self.handles = []
            super().__init__()
            p = c.c_void_p
            for name, args in (
                ("SecKeychainOpen", [c.c_char_p, c.POINTER(p)]),
                ("SecKeychainItemCopyKeychain", [p, c.POINTER(p)]),
                ("SecKeychainGetPath", [p, c.POINTER(c.c_uint32), p]),
            ):
                self._bind(self.sec, name, args, c.c_int32)
            self._bind(self.cf, "CFArrayCreate", [p, p, c.c_long, p], p)
            self._bind(self.cf, "CFDictionaryCreateMutableCopy", [p, c.c_long, p], p)
            self._bind(self.cf, "CFDictionarySetValue", [p, p, p], None)
            try:
                for path in allowed_paths:
                    handle = p()
                    if self.sec.SecKeychainOpen(os.fsencode(path), c.byref(handle)):
                        raise KeychainError("fixture scope unavailable")
                    self.handles.append(handle)
            except BaseException:
                self.close()
                raise

        def close(self):
            for handle in self.handles:
                self.cf.CFRelease(handle)
            self.handles.clear()

        def _encode(self, value):
            encoded = super()._encode(value)
            if not isinstance(value, dict) or self.key("kSecClass") not in value:
                return encoded
            if not self.handles:
                self.cf.CFRelease(encoded)
                raise KeychainError("fixture scope unavailable")
            p = c.c_void_p
            array = self.cf.CFArrayCreate(
                None, (p * len(self.handles))(*self.handles), len(self.handles), None
            )
            query = self.cf.CFDictionaryCreateMutableCopy(None, 0, encoded)
            self.cf.CFRelease(encoded)
            try:
                for name, value in (
                    ("kSecMatchSearchList", array),
                    ("kSecUseKeychain", self.handles[0]),
                ):
                    key = super()._encode(self.key(name))
                    try:
                        self.cf.CFDictionarySetValue(query, key, value)
                    finally:
                        self.cf.CFRelease(key)
                return query
            finally:
                self.cf.CFRelease(array)

        def _assert_reference_scope(self, reference):
            """A persistent reference may bypass a search list; verify its actual owner."""
            p = c.c_void_p
            query = self._encode(
                {
                    self.key("kSecClass"): self.key("kSecClassGenericPassword"),
                    self.key("kSecValuePersistentRef"): reference,
                    self.key("kSecReturnRef"): True,
                    self.key("kSecUseDataProtectionKeychain"): False,
                }
            )
            item, keychain = p(), p()
            try:
                code = self.sec.SecItemCopyMatching(query, c.byref(item))
                if code:
                    raise KeychainError(
                        "missing" if code == -25300 else "fixture scope unavailable"
                    )
                if self.sec.SecKeychainItemCopyKeychain(item, c.byref(keychain)):
                    raise KeychainError("fixture scope unavailable")
                buffer, size = c.create_string_buffer(4096), c.c_uint32(4096)
                if self.sec.SecKeychainGetPath(keychain, c.byref(size), buffer):
                    raise KeychainError("fixture scope unavailable")
                if Path(os.fsdecode(buffer.value)).resolve() not in allowed_paths:
                    raise KeychainError("fixture scope unavailable")
            finally:
                for ref in (keychain, item, query):
                    if ref:
                        self.cf.CFRelease(ref)

        def _call(self, operation, query, values):
            validate_scope(root, paths)
            if "persistent" in query:
                self._assert_reference_scope(query["persistent"])
            result = super()._call(operation, query, values)
            if operation == "copy" and "persistent" not in query:
                for match in result:
                    self._assert_reference_scope(match["persistent"])
            return result

    return ScopedAPI()


def native_snapshot() -> dict[str, Any]:
    """Metadata only; caller keeps paths private and reports equality booleans."""
    from scripts.sidecar_pkg.macos_keychain_store import _NativeAPI

    api, p = _NativeAPI(), c.c_void_p
    for name, args in (
        ("SecKeychainCopyDefault", [c.POINTER(p)]),
        ("SecKeychainCopySearchList", [c.POINTER(p)]),
        ("SecKeychainGetPath", [p, c.POINTER(c.c_uint32), p]),
        ("SecKeychainGetStatus", [p, c.POINTER(c.c_uint32)]),
    ):
        api._bind(api.sec, name, args, c.c_int32)

    def info(keychain):
        buffer, size, status = c.create_string_buffer(4096), c.c_uint32(4096), c.c_uint32()
        if api.sec.SecKeychainGetPath(keychain, c.byref(size), buffer):
            raise RuntimeError("Keychain metadata unavailable")
        if api.sec.SecKeychainGetStatus(keychain, c.byref(status)):
            raise RuntimeError("Keychain metadata unavailable")
        return {"path": os.fsdecode(buffer.value), "unlocked": bool(status.value & 1)}

    default, search = p(), p()
    try:
        with api._without_interaction():
            if api.sec.SecKeychainCopyDefault(c.byref(default)):
                raise RuntimeError("Keychain metadata unavailable")
            if api.sec.SecKeychainCopySearchList(c.byref(search)):
                raise RuntimeError("Keychain metadata unavailable")
            return {
                "default": info(default),
                "search_list": [
                    info(api.cf.CFArrayGetValueAtIndex(search, i))
                    for i in range(api.cf.CFArrayGetCount(search))
                ],
            }
    finally:
        for ref in (search, default):
            if ref:
                api.cf.CFRelease(ref)
