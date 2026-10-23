"""Setup orchestration tests use injected subprocess/native substitutes only."""

import sys
from unittest.mock import Mock

import pytest

from scripts.sidecar_pkg import source_consent as consent
from scripts.sidecar_pkg.source_setup import SetupController


def test_off_never_spawns_authorization(monkeypatch):
    monkeypatch.setattr(consent, "POLICY", consent.SourcePolicy({}))
    launch = Mock()
    controller = SetupController(launch=launch, verify=Mock())
    assert controller.start("anthropic.claude-code") == "Off"
    launch.assert_not_called()


def test_single_flight_and_fresh_verification(monkeypatch):
    import threading

    monkeypatch.setattr(
        consent, "POLICY", consent.SourcePolicy(consent.SourcePolicy.enable_all({}))
    )
    entered, release = threading.Event(), threading.Event()

    def launch(source_id):
        entered.set()
        assert release.wait(2)
        return True

    verify = Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    assert controller.start("anthropic.claude-code") == "Connecting"
    assert entered.wait(2)
    assert controller.start("anthropic.chrome") == "Connecting"
    release.set()
    controller.wait(2)
    verify.assert_called_once()
    assert controller.state("anthropic.claude-code") == "Limited"


@pytest.mark.parametrize("success", [True, False])
def test_disable_during_setup_drops_result(monkeypatch, success):
    import threading

    config = consent.SourcePolicy.enable_all({})
    monkeypatch.setattr(consent, "POLICY", consent.SourcePolicy(config))
    entered, release = threading.Event(), threading.Event()

    def launch(source_id):
        entered.set()
        assert release.wait(2)
        return success

    verify = Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    controller.start("anthropic.chrome")
    assert entered.wait(2)
    consent.POLICY.configure(consent.SourcePolicy.change(config, "anthropic.chrome", read=False))
    release.set()
    controller.wait(2)
    verify.assert_not_called()
    assert controller.state("anthropic.chrome") == "Off"


@pytest.mark.parametrize(
    "matches, readable, expected",
    [(1, True, True), (2, True, False), (0, True, False), (1, False, False)],
)
def test_fresh_verification_exact_item_and_cleanup(monkeypatch, matches, readable, expected):
    from scripts.sidecar_pkg import keychain_client, source_setup

    config = consent.SourcePolicy.enable_all({})
    monkeypatch.setattr(consent, "POLICY", consent.SourcePolicy(config))
    item = {
        "persistent": b"synthetic-ref",
        "service": "Chrome Safe Storage",
        "account": "synthetic-user",
    }
    client = Mock()
    client._timeout = 30

    def call(operation, query, values, **kwargs):
        assert kwargs["allowed"]()
        if "service" in query:
            return [item] * matches
        return {**item, "data": b"synthetic" if readable else b""}

    client.call.side_effect = call
    monkeypatch.setattr(keychain_client, "KeychainClient", lambda: client)
    assert (
        source_setup.verify_background(
            "anthropic.chrome", consent.POLICY.ticket("anthropic.chrome")
        )
        is expected
    )
    client.close.assert_called_once()
    assert client.call.call_count == (2 if matches == 1 else 1)


def test_authorization_child_says_authorization_requires_macos(monkeypatch, capsys):
    """The refusal names the reason, so running the entrypoint by hand says why."""
    from scripts.sidecar_pkg import source_setup

    monkeypatch.setattr(source_setup.sys, "platform", "win32")
    monkeypatch.setattr(source_setup.sys, "frozen", True, raising=False)

    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])

    assert result.value.code == 1
    error = capsys.readouterr().err
    assert "anthropic.chrome" in error
    assert "requires macOS" in error


def test_authorization_child_says_authorization_requires_the_packaged_build(monkeypatch, capsys):
    """Not frozen is its own reason: a developer on macOS is not on macOS-only yet."""
    from scripts.sidecar_pkg import source_setup

    monkeypatch.setattr(source_setup.sys, "platform", "darwin")
    monkeypatch.delattr(source_setup.sys, "frozen", raising=False)

    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])

    assert result.value.code == 1
    error = capsys.readouterr().err
    assert "anthropic.chrome" in error
    assert "requires the packaged build" in error


def test_authorization_child_writes_the_refusal_when_there_is_no_console(monkeypatch, tmp_path):
    """A windowed build has no stderr, so the reason goes to a file the user can find."""
    from scripts.sidecar_pkg import source_setup
    from sidecar_app import config

    monkeypatch.setattr(source_setup.sys, "platform", "darwin")
    monkeypatch.setattr(source_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(source_setup.sys, "stderr", None)
    monkeypatch.setattr(source_setup, "_attach_parent_console", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(source_setup, "_notify_windows", messages.append)
    path = tmp_path / "config.json"
    config.save_local_config(path, {})
    monkeypatch.setattr(config, "get_config_path", lambda: path)

    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])

    assert result.value.code == 1
    refusal = path.parent / "source_authorization.txt"
    assert "anthropic.chrome" in refusal.read_text(encoding="utf-8")
    assert messages and "source_authorization.txt" in messages[0]


def test_authorization_child_reports_a_denied_keychain_read(monkeypatch, tmp_path, capsys):
    """A denied read is reported with its own reason, not as an opaque failure."""
    from scripts.sidecar_pkg import keychain_authorization, macos_keychain_store, source_setup
    from scripts.sidecar_pkg.keychain_protocol import KeychainError
    from sidecar_app import config

    monkeypatch.setattr(source_setup.sys, "platform", "darwin")
    monkeypatch.setattr(source_setup.sys, "frozen", True, raising=False)
    path = tmp_path / "config.json"
    config.save_local_config(path, consent.SourcePolicy.enable_all({}))
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(macos_keychain_store, "_NativeAPI", lambda: object())
    monkeypatch.setattr(
        keychain_authorization,
        "authorize_once",
        Mock(side_effect=KeychainError("denied", native_status=-128)),
    )

    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])

    assert result.value.code == 1
    error = capsys.readouterr().err
    assert "anthropic.chrome" in error
    assert "denied" in error
    assert "OSStatus -128" in error


def test_authorization_child_checks_local_permission_before_native(monkeypatch, tmp_path, capsys):
    from scripts.sidecar_pkg import macos_keychain_store, source_setup
    from sidecar_app import config

    monkeypatch.setattr(source_setup.sys, "platform", "darwin")
    monkeypatch.setattr(source_setup.sys, "frozen", True, raising=False)
    path = tmp_path / "config.json"
    config.save_local_config(path, {})
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    native = Mock(side_effect=AssertionError("must not load frameworks"))
    monkeypatch.setattr(macos_keychain_store, "_NativeAPI", native)
    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])
    assert result.value.code == 1
    native.assert_not_called()
    assert "anthropic.chrome" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="native scope uses POSIX ownership")
def test_authorization_child_uses_only_catalogued_resource(monkeypatch, tmp_path):
    from scripts.sidecar_pkg import keychain_authorization, macos_keychain_store, source_setup
    from sidecar_app import config

    monkeypatch.setattr(source_setup.sys, "platform", "darwin")
    monkeypatch.setattr(source_setup.sys, "frozen", True, raising=False)
    path = tmp_path / "config.json"
    config.save_local_config(path, consent.SourcePolicy.enable_all({}))
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    native = object()
    monkeypatch.setattr(macos_keychain_store, "_NativeAPI", lambda: native)
    authorize = Mock()
    monkeypatch.setattr(keychain_authorization, "authorize_once", authorize)
    with pytest.raises(SystemExit) as result:
        source_setup.authorization_command(["--authorize-keychain-source", "anthropic.chrome"])
    assert result.value.code == 0
    authorize.assert_called_once_with(native, ["Chrome Safe Storage"], None)
