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
    adapter = store.KeychainStore(api, allowed=lambda: True)
    item = adapter.find("service", "user")
    assert item.reference == b"exact"
    assert api.calls[0][1]["account"] == "user"
    api.matches *= 2
    with pytest.raises(store.KeychainError, match="ambiguous"):
        adapter.find("service", "user")


def test_update_only_changes_data_on_exact_item():
    api = API()
    adapter = store.KeychainStore(api, allowed=lambda: True)
    adapter.update(b"exact", b"new-data")
    assert api.calls == [("update", {"persistent": b"exact"}, {"data": b"new-data"})]


def test_journal_is_keychain_only_and_verifies_write():
    api = API()
    adapter = store.KeychainStore(api, allowed=lambda: True)
    adapter.save_journal(b"target", {"phase": "intent", "payload": "synthetic"})
    assert api.calls[0][1]["service"] == store.RECOVERY_SERVICE
    assert not any("target" in str(call) for call in api.calls)
    assert adapter.read_journal(b"target") == {"phase": "intent", "payload": "synthetic"}


def test_native_api_is_lazy_and_unavailable_off_darwin(monkeypatch):
    monkeypatch.setattr(store.sys, "platform", "linux")
    with pytest.raises(store.KeychainError, match="unsupported"):
        store.KeychainStore()


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
    api._write_policy_supported = lambda reference: True
    api.cf = SimpleNamespace(CFRelease=lambda value: None)

    def copy(query, output):
        calls.append(("copy", query))
        return status

    def update(query, values):
        calls.append(("update", query, values))
        return status

    api.sec = SimpleNamespace(
        SecItemCopyMatching=copy,
        SecItemUpdate=update,
        SecKeychainGetUserInteractionAllowed=lambda ptr: 0,
        SecKeychainSetUserInteractionAllowed=lambda state: 0,
    )
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


def test_explicit_authorization_restores_policy_and_reads_only_exact_reference():
    api, calls = native_stub({"data": b"synthetic-private"})
    policy = []

    def previous(output):
        output._obj.value = 0
        return 0

    api.sec.SecKeychainGetUserInteractionAllowed = previous
    api.sec.SecKeychainSetUserInteractionAllowed = lambda value: policy.append(bool(value)) or 0
    assert api.authorize_read(b"exact") == {"data": b"synthetic-private"}
    assert policy == [True, False]
    assert calls == [
        (
            "copy",
            {
                "kSecValuePersistentRef": b"exact",
                "kSecClass": "kSecClassGenericPassword",
                "kSecUseDataProtectionKeychain": False,
                "kSecReturnAttributes": True,
                "kSecReturnPersistentRef": True,
                "kSecReturnData": True,
            },
        )
    ]


def test_explicit_authorization_denial_retains_numeric_status_and_restores_policy():
    api, calls = native_stub(status=-25308)
    policy = []
    api.sec.SecKeychainSetUserInteractionAllowed = lambda value: policy.append(bool(value)) or 0
    with pytest.raises(store.KeychainError) as caught:
        api.authorize_read(b"exact")
    assert caught.value.reason == "denied"
    assert caught.value.native_status == -25308
    assert str(caught.value) == "denied"
    assert policy == [True, False]
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["read_policy", "enable", "restore"])
def test_explicit_authorization_policy_failure_is_closed(failure):
    api, calls = native_stub()
    api.sec.SecKeychainGetUserInteractionAllowed = lambda output: int(failure == "read_policy")

    def policy(enabled):
        return int(failure == ("enable" if enabled else "restore"))

    api.sec.SecKeychainSetUserInteractionAllowed = policy
    with pytest.raises(store.KeychainError, match="^unavailable$"):
        api.authorize_read(b"exact")
    assert len(calls) == int(failure == "restore")


def test_source_helper_runs_outside_repository_without_credential_io(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from scripts.sidecar_pkg import keychain_worker
    from scripts.sidecar_pkg.keychain_client import encode_frame

    result = subprocess.run(
        [sys.executable, str(Path(keychain_worker.__file__).resolve())],
        input=encode_frame({"operation": "invalid", "query": {}, "values": {}}),
        capture_output=True,
        cwd=tmp_path,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == b"" and result.stderr == b""


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
    adapter = store.KeychainStore(api, allowed=lambda: True)
    with pytest.raises(store.KeychainError, match="identity changed"):
        adapter.find("unexpected-service", "user")


@pytest.mark.parametrize("operation", ["copy", "update", "add", "delete"])
def test_native_calls_disable_and_restore_interaction(operation):
    api, calls = native_stub([])
    state = [1]
    events = []

    def get_state(ptr):
        ptr._obj.value = state[0]
        return 0

    def set_state(value):
        events.append(value)
        state[0] = value
        return 0

    api.sec.SecKeychainGetUserInteractionAllowed = get_state
    api.sec.SecKeychainSetUserInteractionAllowed = set_state

    def native(*args):
        assert state[0] == 0
        return 0

    api.sec.SecItemCopyMatching = native
    api.sec.SecItemUpdate = native
    api.sec.SecItemAdd = native
    api.sec.SecItemDelete = native
    values = {"service": store.RECOVERY_SERVICE} if operation == "add" else {}
    api.call(operation, {"persistent": b"exact"}, values)
    assert events == [False, True]
    assert state == [1]


def test_native_interaction_policy_failure_prevents_item_access():
    api, calls = native_stub([])
    api.sec.SecKeychainGetUserInteractionAllowed = lambda ptr: -50
    with pytest.raises(store.KeychainError, match="unavailable"):
        api.call("copy", {"persistent": b"exact"}, {})
    assert calls == []


def test_native_interaction_required_is_permission_denial():
    api, _ = native_stub(status=-25315)
    api.sec.SecKeychainGetUserInteractionAllowed = lambda ptr: 0
    api.sec.SecKeychainSetUserInteractionAllowed = lambda value: 0
    with pytest.raises(store.KeychainError, match="^denied$"):
        api.call("copy", {"persistent": b"exact"}, {})


@pytest.mark.parametrize("failure", ["disable", "restore"])
def test_interaction_policy_failures_are_closed(failure):
    api, calls = native_stub([])

    def get_state(ptr):
        ptr._obj.value = 1
        return 0

    def set_state(value):
        return -50 if (bool(value) == (failure == "restore")) else 0

    api.sec.SecKeychainGetUserInteractionAllowed = get_state
    api.sec.SecKeychainSetUserInteractionAllowed = set_state
    with pytest.raises(store.KeychainError, match="unavailable"):
        api.call("copy", {"persistent": b"exact"}, {})
    if failure == "disable":
        assert calls == []


def test_service_only_lookup_rejects_ambiguity():
    api = API()
    adapter = store.KeychainStore(api)
    assert adapter.read_secret("service") == api.data
    assert api.calls[0] == ("copy", {"service": "service"})
    api.matches *= 2
    with pytest.raises(store.KeychainError, match="ambiguous"):
        adapter.read_secret("service")


def test_native_update_refuses_unsafe_policy_before_changing_data():
    api, calls = native_stub()
    api._write_policy_supported = lambda reference: False
    with pytest.raises(store.KeychainError, match="policy unavailable"):
        api.call("update", {"persistent": b"exact"}, {"data": b"replacement"})
    assert calls == []


@pytest.mark.parametrize(
    "partitions,integrity,current,supported",
    [
        ([], False, None, True),
        ([], True, "cdhash:synthetic-writer", False),
        ([["cdhash:synthetic-writer"]], True, "cdhash:synthetic-writer", True),
        ([["cdhash:synthetic-owner"]], True, "cdhash:synthetic-writer", False),
        ([["cdhash:synthetic-writer", "apple-tool:"]], True, "cdhash:synthetic-writer", False),
        ([["cdhash:synthetic-writer"]], True, None, False),
        ([["teamid:synthetic-writer"]], True, "teamid:synthetic-writer", True),
        (
            [["cdhash:synthetic-writer"], ["cdhash:synthetic-writer"]],
            True,
            "cdhash:synthetic-writer",
            False,
        ),
    ],
)
def test_partition_write_precondition(partitions, integrity, current, supported):
    assert store._partition_write_supported(partitions, integrity, current) is supported


@pytest.mark.parametrize("operation", ["copy", "update", "delete", "check_update", "add"])
def test_explicit_keychain_scope_is_consumed_before_native_dispatch(operation):
    import contextlib

    api, unused = native_stub()
    events = []

    @contextlib.contextmanager
    def scope(paths):
        events.append(("open_scope", paths))
        yield
        events.append(("close_scope", paths))

    api._keychain_scope = scope
    api._assert_reference_scope = lambda ref: events.append(("check_reference", ref))
    api._call = lambda op, query, values: events.append((op, query, values))
    api.call(operation, {"persistent": b"fixture", "keychains": ["/private/fixture"]}, {})
    assert events == [
        ("open_scope", ["/private/fixture"]),
        ("check_reference", b"fixture"),
        (operation, {"persistent": b"fixture"}, {}),
        ("close_scope", ["/private/fixture"]),
    ]


def test_foreign_reference_never_reaches_scoped_credential_operation():
    import contextlib

    api, unused = native_stub()
    api._keychain_scope = lambda paths: contextlib.nullcontext()
    calls = []
    api._call = lambda *args: calls.append(args)

    def reject(reference):
        raise store.KeychainError("unavailable")

    api._assert_reference_scope = reject
    with pytest.raises(store.KeychainError, match="unavailable"):
        api.call("copy", {"persistent": b"foreign", "keychains": ["/private/fixture"]}, {})
    assert not calls


def test_scoped_discovery_validates_each_reference_before_returning():
    import contextlib

    api, unused = native_stub()
    api._keychain_scope = lambda paths: contextlib.nullcontext()
    api._call = lambda *args: [{"persistent": b"one"}, {"persistent": b"two"}]
    checked = []
    api._assert_reference_scope = checked.append
    assert len(api.call("copy", {"service": "synthetic", "keychains": ["fixture"]}, {})) == 2
    assert checked == [b"one", b"two"]


@pytest.mark.parametrize(
    "paths", [None, [], "fixture", [None], ["relative.keychain"], ["/missing.keychain"]]
)
def test_invalid_explicit_keychain_scope_never_opens_native_file(paths):
    api, calls = native_stub()
    with pytest.raises(store.KeychainError, match="unavailable"):
        api.call("copy", {"service": "synthetic", "keychains": paths}, {})
    assert not calls


@pytest.mark.skipif(store.sys.platform == "win32", reason="native scope uses POSIX ownership")
@pytest.mark.parametrize("failure", ["none", "second_open", "operation"])
def test_scope_releases_handles_and_resets_state_on_every_exit(tmp_path, failure):
    from types import SimpleNamespace

    api, unused = native_stub()
    paths = [tmp_path / "one.keychain", tmp_path / "two.keychain"]
    for path in paths:
        path.touch()
    opened, released = [], []

    def open_keychain(path, output):
        number = len(opened) + 1
        output._obj.value = number
        opened.append(number)
        return -50 if failure == "second_open" and number == 2 else 0

    api.sec.SecKeychainOpen = open_keychain
    api.cf = SimpleNamespace(CFRelease=lambda value: released.append(value.value))
    try:
        with api._keychain_scope([str(path) for path in paths]):
            assert [value.value for value in api._scope_keychains] == [1, 2]
            if failure == "operation":
                raise store.KeychainError("synthetic")
    except store.KeychainError:
        assert failure != "none"
    assert released == [2, 1]
    assert api._scope_keychains is None


def test_foreign_scope_ownership_check_never_requests_data():
    import ctypes
    from types import SimpleNamespace

    api, calls = native_stub()
    api._scope_keychains = [ctypes.c_void_p(1)]
    api._encode_query = lambda value: value
    released = []

    def resolve(query, output):
        calls.append(("metadata", query))
        output._obj.value = 2
        return 0

    def owner(item, output):
        output._obj.value = 3
        return 0

    api.sec.SecItemCopyMatching = resolve
    api.sec.SecKeychainItemCopyKeychain = owner
    api.cf = SimpleNamespace(CFRelease=released.append, CFEqual=lambda *args: False)
    with pytest.raises(store.KeychainError, match="unavailable"):
        api._assert_reference_scope(b"foreign")
    assert len(calls) == 1 and calls[0][1]["kSecReturnRef"] is True
    assert "kSecReturnData" not in calls[0][1]
    assert len(released) == 3
