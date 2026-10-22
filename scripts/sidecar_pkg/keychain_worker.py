"""Private noninteractive Keychain worker. No desktop initialization or credential logs."""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

# Source helpers must also bootstrap when the caller's cwd is outside the repository.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.sidecar_pkg.keychain_protocol import MAX_FRAME, OPERATIONS, TIMEOUT, encode_frame
from scripts.sidecar_pkg.macos_keychain_store import KeychainError, _NativeAPI


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def _requests(stream: BinaryIO, requests: queue.Queue[dict[str, Any]]) -> None:
    import plistlib

    try:
        while True:
            size = struct.unpack(">I", _read_exact(stream, 4))[0]
            if not 0 < size <= MAX_FRAME:
                break
            value = plistlib.loads(_read_exact(stream, size))
            if not isinstance(value, dict) or set(value) != {"operation", "query", "values"}:
                break
            if (
                value["operation"] not in OPERATIONS
                or not isinstance(value["query"], dict)
                or not isinstance(value["values"], dict)
            ):
                break
            requests.put(value, timeout=TIMEOUT)
    except Exception:
        pass  # Malformed input and EOF are private protocol failures, never diagnostics.
    # Independent reader detects parent loss even while the main thread is in Security.
    os._exit(0)


def worker_main() -> None:
    requests: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    threading.Thread(target=_requests, args=(sys.stdin.buffer, requests), daemon=True).start()
    native = None
    while True:
        request = requests.get()
        watchdog = threading.Timer(TIMEOUT, os._exit, args=(1,))
        watchdog.daemon = True
        watchdog.start()
        try:
            if native is None:
                native = _NativeAPI()
            result = native.call(request["operation"], request["query"], request["values"])
            response = {"result": result} if result is not None else {}
        except KeychainError as exc:
            response = {"error": exc.reason}
        except Exception:
            response = {"error": "unavailable"}
        try:
            sys.stdout.buffer.write(encode_frame(response))
            sys.stdout.buffer.flush()
        except Exception:
            return
        finally:
            watchdog.cancel()


if __name__ == "__main__":
    worker_main()
