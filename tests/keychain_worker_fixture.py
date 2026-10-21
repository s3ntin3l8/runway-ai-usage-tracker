"""Synthetic persistent credential store for subprocess recovery integration tests only.

All values come from public test fixtures. Never instantiate the native Security API.
"""

import os
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.sidecar_pkg import keychain_worker
from scripts.sidecar_pkg.keychain_protocol import KeychainError

snapshot = Path(sys.argv[1])
fault = Path(sys.argv[2])


class API:
    def call(self, operation, query, values):
        items = plistlib.loads(snapshot.read_bytes())
        mode = fault.read_text() if fault.exists() else ""
        stage = ""
        if operation == "copy":
            if "persistent" in query:
                ref = query["persistent"].decode()
                if ref not in items:
                    raise KeychainError("missing")
                return items[ref]
            return [
                {"persistent": ref.encode()}
                for ref, item in items.items()
                if item["service"] == query["service"]
                and ("account" not in query or item["account"] == query["account"])
            ]
        if operation == "add":
            stage = "intent"
            if mode == "intent_before_save":
                fault.unlink()
                raise KeychainError("failed")
            items["journal"] = {**values, "persistent": b"journal"}
        elif operation == "update":
            ref = query["persistent"].decode()
            stage = "pending" if ref == "journal" else "target"
            if mode == stage + "_before_save":
                fault.unlink()
                os._exit(1)
            if mode == stage + "_failed":
                fault.unlink()
                raise KeychainError("failed")
            items[ref].update(values)
        elif operation == "delete":
            stage = "cleanup"
            del items[query["persistent"].decode()]
        snapshot.write_bytes(plistlib.dumps(items))
        if mode == stage + "_after_save":
            fault.unlink()
            os._exit(1)
        return None


keychain_worker._NativeAPI = API
keychain_worker.worker_main()
