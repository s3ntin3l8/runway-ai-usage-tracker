"""Safety boundaries for the explicit native acceptance harness (no Keychain IO)."""

from pathlib import Path

import pytest

from tests import native_claude_keychain_acceptance as acceptance


def test_harness_never_changes_user_preferences_or_locks_keychains():
    source = Path(acceptance.__file__).read_text()
    forbidden = ("SecKeychainSetDefault", "SecKeychainSetSearchList", "SecKeychainLock")
    assert all(name not in source for name in forbidden)


def test_packaged_execution_rejected_before_creating_fixture(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(acceptance, "Fixture", lambda *a: pytest.fail("fixture created"))
    with pytest.raises(ValueError, match="scoped source worker"):
        acceptance.main(SimpleNamespace(source_native_io=False))


def test_validate_scope_rejects_login_outside_fixture(tmp_path):
    from tests.native_keychain_isolation import validate_scope

    root = tmp_path / "runway-native-526-test"
    root.mkdir(mode=0o700)
    login = tmp_path / "login.keychain-db"
    login.touch()
    with pytest.raises(ValueError, match="fixture scope"):
        validate_scope(root, [login])


def test_validate_scope_rejects_symlink_and_empty_list(tmp_path):
    from tests.native_keychain_isolation import validate_scope

    root = tmp_path / "runway-native-526-test"
    root.mkdir(mode=0o700)
    outside = tmp_path / "other.keychain"
    outside.touch()
    link = root / "fixture-0.keychain"
    link.symlink_to(outside)
    for paths in ([], [link]):
        with pytest.raises(ValueError, match="fixture scope"):
            validate_scope(root, paths)


def test_validate_scope_accepts_only_private_regular_fixture_files(tmp_path):
    from tests.native_keychain_isolation import validate_scope

    root = tmp_path / "runway-native-526-test"
    root.mkdir(mode=0o700)
    path = root / "fixture-0.keychain"
    path.touch()
    assert validate_scope(root, [path]) == (path,)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="fixture scope"):
        validate_scope(root, [path])


@pytest.fixture
def scoped_stub(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from scripts.sidecar_pkg import macos_keychain_store as store
    from tests.native_keychain_isolation import scoped_api

    root = tmp_path / "runway-native-526-test"
    root.mkdir(mode=0o700)
    path = root / "fixture-0.keychain"
    path.touch()
    owner = {"path": path}
    calls = Mock(return_value=None)

    def output(value):
        def set_output(*args):
            args[-1]._obj.value = value
            return 0

        return set_output

    def get_path(handle, size, buffer):
        buffer.value = str(owner["path"]).encode()
        return 0

    class Native:
        def __init__(self):
            self.sec = SimpleNamespace(
                SecKeychainOpen=output(11),
                SecItemCopyMatching=output(12),
                SecKeychainItemCopyKeychain=output(13),
                SecKeychainGetPath=get_path,
            )
            self.cf = SimpleNamespace(
                CFArrayCreate=lambda *args: list(args[1]),
                CFDictionaryCreateMutableCopy=lambda *args: dict(args[-1]),
                CFDictionarySetValue=lambda query, key, value: query.__setitem__(key, value),
                CFRelease=Mock(),
            )

        def _bind(self, *unused):
            pass

        def key(self, name):
            return name

        def _encode(self, value):
            return value

        def _call(self, *args):
            return calls(*args)

    monkeypatch.setattr(store, "_NativeAPI", Native)
    api = scoped_api(root, [path])
    yield SimpleNamespace(api=api, owner=owner, calls=calls, root=root, path=path)
    api.close()


def test_foreign_persistent_reference_is_rejected_before_data_operation(scoped_stub):
    from scripts.sidecar_pkg.macos_keychain_store import KeychainError

    scoped_stub.owner["path"] = scoped_stub.root.parent / "login.keychain-db"
    for operation in ("copy", "update", "delete", "check_update"):
        with pytest.raises(KeychainError, match="fixture scope unavailable"):
            scoped_stub.api._call(operation, {"persistent": b"foreign"}, {})
    scoped_stub.calls.assert_not_called()


def test_every_native_item_query_has_explicit_fixture_keychains(scoped_stub):
    query = scoped_stub.api._encode({"kSecClass": "generic", "kSecReturnRef": True})
    assert query["kSecUseKeychain"].value == 11
    assert query["kSecMatchSearchList"] == [11]
    assert "kSecReturnData" not in query


def test_validated_reference_reaches_data_operation(scoped_stub):
    scoped_stub.api._call("update", {"persistent": b"fixture"}, {"data": b"synthetic"})
    scoped_stub.calls.assert_called_once_with(
        "update", {"persistent": b"fixture"}, {"data": b"synthetic"}
    )
