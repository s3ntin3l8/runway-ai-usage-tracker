"""Keychain adapter contract; no test may load a real credential store."""

import pytest

from scripts.sidecar_pkg import macos_keychain_store as store


class API:
    def __init__(self):
        self.calls = []
        self.matches = [{"persistent": b"exact"}]
        self.data = b'{"payload": "synthetic"}'
        self.journal = None

    def copy(self, query):
        self.calls.append(("copy", query))
        if query.get("persistent") == b"journal":
            return {
                "data": self.journal["data"],
                "persistent": b"journal",
                "service": self.journal["service"],
                "account": self.journal["account"],
            }
        if query.get("persistent"):
            return {
                "data": self.data,
                "persistent": b"exact",
                "service": "service",
                "account": "user",
            }
        if query.get("service") == store.RECOVERY_SERVICE:
            return [{"persistent": b"journal"}] if self.journal else []
        return self.matches

    def update(self, query, values):
        self.calls.append(("update", query, values))
        if query["persistent"] == b"journal":
            self.journal["data"] = values["data"]
        else:
            self.data = values["data"]

    def add(self, values):
        self.calls.append(("add", values))
        self.journal = values.copy()

    def delete(self, query):
        self.calls.append(("delete", query))


def test_find_uses_account_and_rejects_ambiguous_results():
    api = API()
    adapter = store.KeychainStore(api)
    item = adapter.find("service", "user")
    assert item.reference == b"exact"
    assert api.calls[0][1]["account"] == "user"
    api.matches *= 2
    with pytest.raises(store.KeychainError, match="ambiguous"):
        adapter.find("service", "user")


def test_update_only_changes_data_on_exact_item():
    api = API()
    adapter = store.KeychainStore(api)
    adapter.update(b"exact", b"new-data")
    assert api.calls == [("update", {"persistent": b"exact"}, {"data": b"new-data"})]


def test_journal_is_keychain_only_and_verifies_write():
    api = API()
    adapter = store.KeychainStore(api)
    adapter.save_journal(b"target", {"phase": "intent", "payload": "synthetic"})
    assert api.calls[0][1]["service"] == store.RECOVERY_SERVICE
    assert not any("target" in str(call) for call in api.calls)
    assert adapter.read_journal(b"target") == {"phase": "intent", "payload": "synthetic"}


def test_native_api_is_lazy_and_unavailable_off_darwin(monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "linux")
    with pytest.raises(store.KeychainError, match="unsupported"):
        store.KeychainStore()


def test_worker_timeout_discards_native_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise store.subprocess.TimeoutExpired(args[0], 30, output=b"private-token")

    monkeypatch.setattr(store.subprocess, "run", timeout)
    with pytest.raises(store.KeychainError, match="^timed_out$"):
        store._WorkerAPI().copy({"persistent": b"exact"})


def test_worker_uses_private_stdin_and_filters_errors(monkeypatch):
    import plistlib

    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return store.subprocess.CompletedProcess(
            command, 0, plistlib.dumps({"error": "private-secret"}), b""
        )

    monkeypatch.setattr(store.subprocess, "run", run)
    with pytest.raises(store.KeychainError, match="^unavailable$"):
        store._WorkerAPI().update({"persistent": b"exact"}, {"data": b"private-token"})
    assert "private-token" not in str(seen[0][0])
    assert plistlib.loads(seen[0][1]["input"])["values"]["data"] == b"private-token"
    assert seen[0][1]["timeout"] == 30


def test_permission_is_rechecked_after_journal_lookup():
    api = API()
    permission = [True]
    original = api.copy

    def revoke(query):
        permission[0] = False
        return original(query)

    api.copy = revoke
    adapter = store.KeychainStore(api, allowed=lambda: permission[0])
    with pytest.raises(store.KeychainError, match="permission disabled"):
        adapter.save_journal(b"target", {"phase": "intent"})
    assert all(call[0] == "copy" for call in api.calls)


def native_stub(result=None, status=0):
    """Exercise native query construction without loading frameworks or reading Keychain."""
    from types import SimpleNamespace

    api = store._NativeAPI.__new__(store._NativeAPI)
    calls = []
    api.key = lambda name: name
    api._encode = lambda value: value
    api._decode_matches = lambda value: result
    api.cf = SimpleNamespace(CFRelease=lambda value: None)

    def copy(query, output):
        calls.append(("copy", query))
        return status

    def update(query, values):
        calls.append(("update", query, values))
        return status

    api.sec = SimpleNamespace(SecItemCopyMatching=copy, SecItemUpdate=update)
    return api, calls


def test_native_enumeration_does_not_request_password_data():
    api, calls = native_stub([{"persistent": b"exact"}])
    assert api.call("copy", {"service": "service", "account": "user"}, {}) == [
        {"persistent": b"exact"}
    ]
    query = calls[0][1]
    assert query["kSecAttrAccount"] == "user"
    assert query["kSecAttrService"] == "service"
    assert "kSecReturnData" not in query
    assert query["kSecMatchLimit"] == "kSecMatchLimitAll"


def test_native_update_has_no_acl_or_identity_mutations():
    api, calls = native_stub()
    api.call("update", {"persistent": b"exact"}, {"data": b"replacement"})
    query, values = calls[0][1:]
    assert query["kSecValuePersistentRef"] == b"exact"
    assert values == {"kSecValueData": b"replacement"}
    assert "kSecReturnAttributes" not in query
    assert "kSecAttrAccount" not in query


@pytest.mark.parametrize(
    "code,reason",
    [
        (-25300, "missing"),
        (-25293, "denied"),
        (-25308, "denied"),
        (-128, "denied"),
        (-50, "failed"),
    ],
)
def test_native_status_is_sanitized(code, reason):
    api, _ = native_stub(status=code)
    with pytest.raises(store.KeychainError, match=f"^{reason}$"):
        api.call("copy", {"persistent": b"exact"}, {})


def test_source_helper_runs_outside_repository_without_credential_io(tmp_path, monkeypatch):
    """An invalid operation exercises bootstrap only, never SecItemCopyMatching."""
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    run = store.subprocess.run
    completed = []

    def record(*args, **kwargs):
        result = run(*args, **kwargs)
        completed.append(result)
        return result

    monkeypatch.setattr(store.subprocess, "run", record)
    with pytest.raises(store.KeychainError, match="unavailable"):
        store._WorkerAPI().call("invalid", {}, {})
    assert completed[0].returncode == 0
    assert Path(completed[0].args[1]).is_absolute()


@pytest.mark.skipif(store.sys.platform != "darwin", reason="memory-only macOS framework test")
def test_native_attributes_ignore_opaque_acl_objects():
    """SecAccessCreate allocates an ACL in memory; no Keychain is opened or read."""
    import ctypes

    api = store._NativeAPI()
    ptr = ctypes.c_void_p
    api._bind(api.sec, "SecAccessCreate", [ptr, ptr, ctypes.POINTER(ptr)], ctypes.c_int32)
    api._bind(
        api.cf,
        "CFDictionaryCreate",
        [ptr, ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.c_long, ptr, ptr],
        ptr,
    )
    allocated = []
    try:
        label = api._encode("synthetic-no-credential-access")
        allocated.append(label)
        access = ptr()
        assert api.sec.SecAccessCreate(label, None, ctypes.byref(access)) == 0
        allocated.append(access)
        names = [
            "kSecAttrAccess",
            "kSecAttrService",
            "kSecAttrAccount",
            "kSecValuePersistentRef",
            "kSecValueData",
        ]
        encoded = [
            api._encode(value)
            for value in ["synthetic-service", "synthetic-account", b"reference", b"data"]
        ]
        allocated.extend(encoded)
        keys = (ptr * len(names))(*(ptr.in_dll(api.sec, name) for name in names))
        values = (ptr * len(names))(access, *encoded)
        dictionary = api.cf.CFDictionaryCreate(None, keys, values, len(names), None, None)
        allocated.append(dictionary)
        assert api._decode_matches(dictionary) == {
            "service": "synthetic-service",
            "account": "synthetic-account",
            "persistent": b"reference",
            "data": b"data",
        }
    finally:
        for value in reversed(allocated):
            api.cf.CFRelease(value)


def test_identity_change_between_lookup_and_read_fails_closed():
    api = API()
    adapter = store.KeychainStore(api)
    with pytest.raises(store.KeychainError, match="identity changed"):
        adapter.find("unexpected-service", "user")
