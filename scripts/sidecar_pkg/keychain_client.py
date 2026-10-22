"""Serialized, bounded private-pipe transport for noninteractive macOS Keychain IO."""

from __future__ import annotations

import atexit
import contextlib
import os
import plistlib
import selectors
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.keychain_protocol import (
    MAX_FRAME,
    OPERATIONS,
    TIMEOUT,
    KeychainError,
    encode_frame,
)


class KeychainClient:
    def __init__(self, *, command: list[str] | None = None, timeout: float = TIMEOUT) -> None:
        self._command = command or (
            [sys.executable, "--runway-keychain-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, str(Path(__file__).with_name("keychain_worker.py").resolve())]
        )
        self._timeout = timeout
        self._request_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._blocked: dict[str, str] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False
        self._generation = 0

    def access_blocked(self, group: str) -> bool:
        with self._state_lock:
            return group in self._blocked

    def block_access(self, group: str, reason: str = "denied") -> None:
        with self._state_lock:
            self._blocked[group] = reason

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            process.wait(timeout=0.5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def _retire(self, process: subprocess.Popen[bytes]) -> None:
        with self._state_lock:
            if self._process is not process:
                return  # Another thread already owns teardown.
            self._process = None
        self._reap(process)

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._generation += 1
            process = self._process
            self._process = None
        if process is not None:
            self._reap(process)

    def reopen(self) -> None:
        """Resume a stopped daemon without clearing any permission suppression."""
        with self._state_lock:
            self._closed = False

    def _cancelled(self, generation: int) -> bool:
        with self._state_lock:
            return self._closed or generation != self._generation

    def _start(self) -> subprocess.Popen[bytes]:
        with self._state_lock:
            if self._closed:
                raise KeychainError("unavailable")
            if self._process is None:
                process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True,
                )
                try:
                    assert process.stdin is not None and process.stdout is not None
                    os.set_blocking(process.stdin.fileno(), False)
                    os.set_blocking(process.stdout.fileno(), False)
                except BaseException:
                    self._reap(process)
                    raise
                self._process = process
            return self._process

    @staticmethod
    def _ready(fd: int, event: int, deadline: float) -> None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise KeychainError("timed_out")
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            if not selector.select(left):
                raise KeychainError("timed_out")

    def _write(self, fd: int, data: bytes, deadline: float) -> None:
        view = memoryview(data)
        while view:
            self._ready(fd, selectors.EVENT_WRITE, deadline)
            try:
                count = os.write(fd, view)
            except BlockingIOError:
                continue
            if count <= 0:
                raise KeychainError("unavailable")
            view = view[count:]

    def _read(self, fd: int, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            self._ready(fd, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(fd, size - len(data))
            except BlockingIOError:
                continue
            if not chunk:
                raise KeychainError("unavailable")
            data.extend(chunk)
        return bytes(data)

    def call(
        self,
        operation: str,
        query: dict[str, Any],
        values: dict[str, Any],
        *,
        group: str,
        allowed: Callable[[], bool] | None = None,
    ) -> Any:
        with self._state_lock:
            if self._closed:
                raise KeychainError("cancelled")
            generation = self._generation
        deadline = time.monotonic() + self._timeout
        if not self._request_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise KeychainError("timed_out")
        process = None
        try:
            if self._cancelled(generation):
                raise KeychainError("cancelled")
            with self._state_lock:
                reason = self._blocked.get(group)
            if reason:
                raise KeychainError(reason)
            if operation not in OPERATIONS:
                raise KeychainError("unavailable")
            frame = encode_frame({"operation": operation, "query": query, "values": values})
            # The caller may have waited behind another operation: check just before dispatch.
            if operation != "copy" and (allowed is None or allowed() is not True):
                raise KeychainError("permission disabled")
            process = self._start()
            assert process.stdin is not None and process.stdout is not None
            if operation != "copy" and (allowed is None or allowed() is not True):
                raise KeychainError("permission disabled")
            self._write(process.stdin.fileno(), frame, deadline)
            size = struct.unpack(">I", self._read(process.stdout.fileno(), 4, deadline))[0]
            if not 0 < size <= MAX_FRAME:
                raise KeychainError("unavailable")
            response = plistlib.loads(self._read(process.stdout.fileno(), size, deadline))
            if not isinstance(response, dict) or set(response) not in (
                {"result"},
                {"error"},
                set(),
            ):
                raise KeychainError("unavailable")
            if "error" in response:
                reason = response["error"]
                raise KeychainError(
                    reason
                    if reason in {"denied", "missing", "failed", "policy unavailable"}
                    else "unavailable"
                )
            return response.get("result")
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
            if self._cancelled(generation):
                raise KeychainError("cancelled") from None
            self.block_access(group, "unavailable")
            if process is not None:
                self._retire(process)
            raise KeychainError("unavailable") from None
        except KeychainError as exc:
            if exc.reason in {"denied", "timed_out", "unavailable"}:
                if self._cancelled(generation):
                    raise KeychainError("cancelled") from None
                self.block_access(group, exc.reason)
                if process is not None:
                    self._retire(process)
            raise
        finally:
            self._request_lock.release()


_CLIENT: KeychainClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_keychain_client() -> KeychainClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = KeychainClient()
        return _CLIENT


def close_keychain_client() -> None:
    with _CLIENT_LOCK:
        client = _CLIENT
    if client is not None:
        client.close()


def reopen_keychain_client() -> None:
    with _CLIENT_LOCK:
        client = _CLIENT
    if client is not None:
        client.reopen()


atexit.register(close_keychain_client)
