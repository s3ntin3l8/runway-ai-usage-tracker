"""Bounded macOS Keychain IO; secrets travel only over private pipes and native APIs.

The helper receives a binary property list on stdin and emits one on stdout. It never
prints errors or token values. Frameworks are loaded only inside the Darwin helper.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import plistlib
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from scripts.sidecar_pkg.keychain_protocol import KeychainError

RECOVERY_SERVICE = "Runway Claude Code renewal"


@dataclass(frozen=True)
class KeychainItem:
    reference: bytes
    data: bytes
    service: str
    account: str


class _WorkerAPI:
    def __init__(
        self, *, group: str = "claude-code", allowed: Callable[[], bool] = lambda: False
    ) -> None:
        self._group = group
        self._allowed = allowed

    def call(self, operation: str, query: dict[str, Any], values: dict[str, Any]) -> Any:
        from scripts.sidecar_pkg.keychain_client import get_keychain_client

        return get_keychain_client().call(
            operation, query, values, group=self._group, allowed=self._allowed
        )

    def copy(self, query: dict[str, Any]) -> Any:
        return self.call("copy", query, {})

    def update(self, query: dict[str, Any], values: dict[str, Any]) -> None:
        self.call("update", query, values)

    def add(self, values: dict[str, Any]) -> None:
        self.call("add", {}, values)

    def delete(self, query: dict[str, Any]) -> None:
        self.call("delete", query, {})


class KeychainStore:
    """Exact-reference updates; the only creatable items are Runway recovery records."""

    def __init__(
        self,
        api: Any = None,
        *,
        allowed: Callable[[], bool] = lambda: False,
        group: str = "claude-code",
    ) -> None:
        if api is None and sys.platform != "darwin":
            raise KeychainError("unsupported")
        self._allowed = allowed
        self.api = api if api is not None else _WorkerAPI(group=group, allowed=allowed)

    def find(self, service: str, account: str | None) -> KeychainItem | None:
        try:
            query = {"service": service}
            if account is not None:
                query["account"] = account
            matches = self.api.copy(query)
        except KeychainError as exc:
            if exc.reason == "missing":
                return None
            raise
        if not matches:
            return None
        if len(matches) != 1:
            raise KeychainError("ambiguous")
        item = self.read(matches[0]["persistent"])
        if item.service != service or (account is not None and item.account != account):
            raise KeychainError("identity changed")
        return item

    def read_secret(self, service: str, account: str | None = None) -> bytes | None:
        item = self.find(service, account)
        return item.data if item is not None else None

    def read(self, reference: bytes) -> KeychainItem:
        item = self.api.copy({"persistent": reference})
        return KeychainItem(reference, item["data"], item["service"], item["account"])

    def update(self, reference: bytes, data: bytes) -> None:
        self._check_write()
        self.api.update({"persistent": reference}, {"data": data})

    def _check_write(self) -> None:
        if self._allowed() is not True:
            raise KeychainError("permission disabled")

    def _journal_account(self, reference: bytes) -> str:
        return hashlib.sha256(reference).hexdigest()

    def read_journal(self, reference: bytes) -> dict[str, Any] | None:
        item = self.find(RECOVERY_SERVICE, self._journal_account(reference))
        if item is None:
            return None
        try:
            value = plistlib.loads(item.data)
        except (ValueError, plistlib.InvalidFileException):
            raise KeychainError("invalid recovery record") from None
        if not isinstance(value, dict):
            raise KeychainError("invalid recovery record")
        return value

    def save_journal(self, reference: bytes, record: dict[str, Any]) -> None:
        item = self.find(RECOVERY_SERVICE, self._journal_account(reference))
        data = plistlib.dumps(record)
        if item is not None:
            self.update(item.reference, data)
        else:
            self._check_write()
            self.api.add(
                {
                    "service": RECOVERY_SERVICE,
                    "account": self._journal_account(reference),
                    "data": data,
                }
            )
        if self.read_journal(reference) != record:
            raise KeychainError("recovery verification failed")

    def delete_journal(self, reference: bytes) -> None:
        item = self.find(RECOVERY_SERVICE, self._journal_account(reference))
        if item is not None:
            self._check_write()
            self.api.delete({"persistent": item.reference})


def read_secret(service: str, account: str | None = None, *, group: str) -> bytes | None:
    """Read-only entrypoint shared by background discovery sources."""
    if service == "Claude Code-credentials" or service.startswith("Claude Code-credentials-"):
        group = "claude-code"
    return KeychainStore(group=group, allowed=lambda: False).read_secret(service, account)


class _NativeAPI:
    """Small CoreFoundation property-list bridge; no third-party runtime dependency."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise KeychainError("unsupported")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr, size, status = ctypes.c_void_p, ctypes.c_long, ctypes.c_int32
        self._bind(self.cf, "CFRelease", [ptr], None)
        self._bind(self.cf, "CFDataCreate", [ptr, ptr, size], ptr)
        self._bind(self.cf, "CFGetTypeID", [ptr], ctypes.c_ulong)
        self._bind(self.cf, "CFArrayGetTypeID", [], ctypes.c_ulong)
        self._bind(self.cf, "CFDictionaryGetTypeID", [], ctypes.c_ulong)
        self._bind(self.cf, "CFArrayGetCount", [ptr], size)
        self._bind(self.cf, "CFArrayGetValueAtIndex", [ptr, size], ptr)
        self._bind(self.cf, "CFDictionaryGetValue", [ptr, ptr], ptr)
        self._bind(self.cf, "CFDataGetLength", [ptr], size)
        self._bind(self.cf, "CFDataGetBytePtr", [ptr], ptr)
        self._bind(self.cf, "CFPropertyListCreateWithData", [ptr, ptr, size, ptr, ptr], ptr)
        self._bind(self.cf, "CFPropertyListCreateData", [ptr, ptr, size, size, ptr], ptr)
        self._bind(self.cf, "CFStringGetCString", [ptr, ptr, size, ctypes.c_uint32], ctypes.c_bool)
        self._bind(self.sec, "SecItemCopyMatching", [ptr, ctypes.POINTER(ptr)], status)
        self._bind(self.sec, "SecItemUpdate", [ptr, ptr], status)
        self._bind(self.sec, "SecItemAdd", [ptr, ptr], status)
        self._bind(self.sec, "SecItemDelete", [ptr], status)
        self._bind(
            self.sec,
            "SecKeychainGetUserInteractionAllowed",
            [ctypes.POINTER(ctypes.c_ubyte)],
            status,
        )
        self._bind(self.sec, "SecKeychainSetUserInteractionAllowed", [ctypes.c_ubyte], status)

    @staticmethod
    def _bind(lib: Any, name: str, args: list[Any], result: Any) -> None:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result

    def key(self, name: str) -> str:
        ptr = ctypes.c_void_p.in_dll(self.sec, name)
        buffer = ctypes.create_string_buffer(1024)
        if not self.cf.CFStringGetCString(ptr, buffer, len(buffer), 0x08000100):
            raise KeychainError("unavailable")
        return buffer.value.decode()

    def _encode(self, value: Any) -> Any:
        raw = plistlib.dumps(value)
        data = self.cf.CFDataCreate(None, raw, len(raw))
        try:
            result = self.cf.CFPropertyListCreateWithData(None, data, 0, None, None)
            if not result:
                raise KeychainError("unavailable")
            return result
        finally:
            self.cf.CFRelease(data)

    def _decode(self, value: Any) -> Any:
        data = self.cf.CFPropertyListCreateData(None, value, 200, 0, None)
        if not data:
            raise KeychainError("unavailable")
        try:
            raw = ctypes.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data))
            return plistlib.loads(raw)
        finally:
            self.cf.CFRelease(data)

    def _decode_matches(self, value: Any) -> Any:
        """Filter native attributes before serialization: ACL refs are not property lists."""
        kind = self.cf.CFGetTypeID(value)
        if kind == self.cf.CFArrayGetTypeID():
            return [
                self._decode_matches(self.cf.CFArrayGetValueAtIndex(value, index))
                for index in range(self.cf.CFArrayGetCount(value))
            ]
        if kind != self.cf.CFDictionaryGetTypeID():
            raise KeychainError("unavailable")
        result = {}
        for key, name in {
            "service": "kSecAttrService",
            "account": "kSecAttrAccount",
            "persistent": "kSecValuePersistentRef",
            "data": "kSecValueData",
        }.items():
            native_key = ctypes.c_void_p.in_dll(self.sec, name)
            item = self.cf.CFDictionaryGetValue(value, native_key)
            if item:
                result[key] = self._decode(item)
        return result

    @contextlib.contextmanager
    def _without_interaction(self) -> Iterator[None]:
        with self._interaction(False):
            yield

    @contextlib.contextmanager
    def _interaction(self, enabled: bool) -> Iterator[None]:
        previous = ctypes.c_ubyte()
        if self.sec.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)):
            raise KeychainError("unavailable")
        try:
            if self.sec.SecKeychainSetUserInteractionAllowed(enabled):
                raise KeychainError("unavailable")
            yield
        finally:
            if self.sec.SecKeychainSetUserInteractionAllowed(bool(previous.value)):
                raise KeychainError("unavailable")

    def call(self, operation: str, query: dict[str, Any], values: dict[str, Any]) -> Any:
        with self._without_interaction():
            return self._call(operation, query, values)

    def authorize_read(self, reference: bytes) -> Any:
        """One foreground read; deliberately absent from the worker protocol."""
        if not isinstance(reference, bytes) or not reference:
            raise KeychainError("unavailable")
        with self._interaction(True):
            return self._call("copy", {"persistent": reference}, {})

    def _call(self, operation: str, query: dict[str, Any], values: dict[str, Any]) -> Any:
        names = {
            "service": "kSecAttrService",
            "account": "kSecAttrAccount",
            "persistent": "kSecValuePersistentRef",
            "data": "kSecValueData",
        }
        native = {self.key(names[k]): v for k, v in query.items()}
        native[self.key("kSecClass")] = self.key("kSecClassGenericPassword")
        # Claude Code uses the traditional file-based Keychain, not the data-protection one.
        native[self.key("kSecUseDataProtectionKeychain")] = False
        if operation == "copy":
            native[self.key("kSecReturnAttributes")] = True
            native[self.key("kSecReturnPersistentRef")] = True
            if "persistent" in query:
                native[self.key("kSecReturnData")] = True
            else:
                native[self.key("kSecMatchLimit")] = self.key("kSecMatchLimitAll")
        attrs = {self.key(names[k]): v for k, v in values.items()}
        if operation == "add":
            if values.get("service") != RECOVERY_SERVICE:
                raise KeychainError("unavailable")
            native.update(attrs)
        q = self._encode(native)
        a = self._encode(attrs)
        result = ctypes.c_void_p()
        try:
            if operation == "copy":
                code = self.sec.SecItemCopyMatching(q, ctypes.byref(result))
            elif operation == "update":
                code = self.sec.SecItemUpdate(q, a)
            elif operation == "add":
                code = self.sec.SecItemAdd(q, None)
            elif operation == "delete":
                code = self.sec.SecItemDelete(q)
            else:
                raise KeychainError("unavailable")
            if code:
                reason = (
                    "missing"
                    if code == -25300
                    else "denied"
                    if code in {-25293, -25308, -25315, -128}
                    else "failed"
                )
                raise KeychainError(reason, native_status=int(code))
            if operation != "copy":
                return None
            return self._decode_matches(result)
        finally:
            if result:
                self.cf.CFRelease(result)
            self.cf.CFRelease(q)
            self.cf.CFRelease(a)
