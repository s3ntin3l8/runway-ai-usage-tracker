"""Bounded macOS Keychain IO; secrets travel only over private pipes and native APIs.

The helper receives a binary property list on stdin and emits one on stdout. It never
prints errors or token values. Frameworks are loaded only inside the Darwin helper.
"""

from __future__ import annotations

import ctypes
import hashlib
import plistlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECOVERY_SERVICE = "Runway Claude Code renewal"
TIMEOUT = 30


class KeychainError(Exception):
    """A sanitized Keychain failure, safe to log without native diagnostic output."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class KeychainItem:
    reference: bytes
    data: bytes
    service: str
    account: str


class _WorkerAPI:
    def call(self, operation: str, query: dict[str, Any], values: dict[str, Any]) -> Any:
        command = (
            [sys.executable, "--runway-keychain-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, str(Path(__file__).resolve())]
        )
        try:
            result = subprocess.run(
                command,
                input=plistlib.dumps({"operation": operation, "query": query, "values": values}),
                capture_output=True,
                timeout=TIMEOUT,
            )
            if result.returncode:
                raise KeychainError("unavailable")
            response = plistlib.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise KeychainError("timed_out") from None
        except (OSError, ValueError, plistlib.InvalidFileException):
            raise KeychainError("unavailable") from None
        if response.get("error"):
            reason = response["error"]
            raise KeychainError(
                reason if reason in {"denied", "missing", "failed"} else "unavailable"
            )
        return response.get("result")

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

    def __init__(self, api: Any = None, *, allowed: Callable[[], bool] = lambda: True) -> None:
        if api is None and sys.platform != "darwin":
            raise KeychainError("unsupported")
        self.api = api if api is not None else _WorkerAPI()
        self._allowed = allowed

    def find(self, service: str, account: str) -> KeychainItem | None:
        try:
            matches = self.api.copy({"service": service, "account": account})
        except KeychainError as exc:
            if exc.reason == "missing":
                return None
            raise
        if not matches:
            return None
        if len(matches) != 1:
            raise KeychainError("ambiguous")
        item = self.read(matches[0]["persistent"])
        if item.service != service or item.account != account:
            raise KeychainError("identity changed")
        return item

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

    def call(self, operation: str, query: dict[str, Any], values: dict[str, Any]) -> Any:
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
                    if code in {-25293, -25308, -128}
                    else "failed"
                )
                raise KeychainError(reason)
            if operation != "copy":
                return None
            return self._decode_matches(result)
        finally:
            if result:
                self.cf.CFRelease(result)
            self.cf.CFRelease(q)
            self.cf.CFRelease(a)


def worker_main() -> None:
    """Private subprocess entrypoint. Never propagate native diagnostics to the parent."""
    try:
        request = plistlib.loads(sys.stdin.buffer.read())
        result = _NativeAPI().call(request["operation"], request["query"], request["values"])
        response = {"result": result} if result is not None else {}
    except KeychainError as exc:
        response = {"error": exc.reason}
    except Exception:
        response = {"error": "unavailable"}
    sys.stdout.buffer.write(plistlib.dumps(response))


if __name__ == "__main__":
    worker_main()
