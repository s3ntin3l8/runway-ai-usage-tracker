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


@pytest.mark.skipif(sys.platform == "win32", reason="native scope uses POSIX ownership")
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
    monkeypatch.setattr(macos_keychain_store, "_NativeAPI", object)
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


# --- authorize everything that is on, one Keychain item at a time -------------------------------


def _enabled(monkeypatch, only=None):
    config = consent.SourcePolicy.enable_all({})
    if only is not None:
        for source_id in list(config["keychain_sources"]):
            if source_id not in only:
                del config["keychain_sources"][source_id]
    monkeypatch.setattr(consent, "POLICY", consent.SourcePolicy(config))
    return config


def test_start_all_launches_once_per_keychain_item_and_verifies_every_source(monkeypatch):
    from scripts.sidecar_pkg.source_catalog import catalog

    _enabled(monkeypatch)
    launch, verify = Mock(return_value=True), Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    ids = list(catalog())
    controller.start_all(ids)
    controller.wait(5)

    items = {catalog()[i].item_key for i in ids}
    assert launch.call_count == len(items) < len(ids)
    assert verify.call_count == len(ids)
    assert {c.args[0] for c in verify.call_args_list} == set(ids)
    assert all(controller.state(i) == "Limited" for i in ids)
    run = controller.run_report()
    assert (run["active"], run["done"], run["total"], run["current"], run["queued"]) == (
        False,
        len(items),
        len(items),
        [],
        [],
    )


def test_start_all_groups_sources_behind_the_same_item_and_launches_the_first(monkeypatch):
    _enabled(monkeypatch)
    launch, verify = Mock(return_value=True), Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    controller.start_all(["anthropic.chrome", "chatgpt.chrome", "anthropic.edge"])
    controller.wait(5)
    # Chrome's two sources share one Safe Storage item; Edge has its own.
    assert [c.args[0] for c in launch.call_args_list] == ["anthropic.chrome", "anthropic.edge"]
    assert verify.call_count == 3


def test_start_all_skips_sources_that_are_off_unknown_or_already_authorized(monkeypatch):
    _enabled(monkeypatch, only={"anthropic.chrome", "anthropic.edge"})
    launch, verify = Mock(return_value=True), Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    controller.start_all(["anthropic.chrome"])
    controller.wait(5)
    launch.reset_mock()
    verify.reset_mock()
    # chrome is authorized now; github is off; the id is unknown.
    controller.start_all(["anthropic.chrome", "anthropic.edge", "github.gh-cli", "nope.nope"])
    controller.wait(5)
    assert [c.args[0] for c in launch.call_args_list] == ["anthropic.edge"]
    assert [c.args[0] for c in verify.call_args_list] == ["anthropic.edge"]


def test_start_all_with_nothing_to_do_does_not_start_and_reports_idle(monkeypatch):
    monkeypatch.setattr(consent, "POLICY", consent.SourcePolicy({}))
    launch = Mock()
    controller = SetupController(launch=launch, verify=Mock())
    run = controller.start_all(["anthropic.chrome"])
    assert run["active"] is False and run["total"] == 0
    launch.assert_not_called()


def test_a_failed_launch_marks_every_source_behind_the_item_action_required(monkeypatch):
    _enabled(monkeypatch)
    verify = Mock(return_value=True)
    controller = SetupController(launch=Mock(return_value=False), verify=verify)
    controller.start_all(["anthropic.chrome", "chatgpt.chrome"])
    controller.wait(5)
    verify.assert_not_called()
    assert controller.state("anthropic.chrome") == "Action required"
    assert controller.state("chatgpt.chrome") == "Action required"


def test_one_source_failing_verification_does_not_fail_its_siblings(monkeypatch):
    _enabled(monkeypatch)
    controller = SetupController(
        launch=Mock(return_value=True),
        verify=lambda source_id, ticket: source_id != "chatgpt.chrome",
    )
    controller.start_all(["anthropic.chrome", "chatgpt.chrome"])
    controller.wait(5)
    assert controller.state("anthropic.chrome") == "Limited"
    assert controller.state("chatgpt.chrome") == "Action required"


def test_an_exception_from_launch_or_verify_is_a_failure_not_a_crash(monkeypatch):
    _enabled(monkeypatch)
    controller = SetupController(launch=Mock(side_effect=OSError("boom")), verify=Mock())
    controller.start_all(["anthropic.chrome"])
    controller.wait(5)
    assert controller.state("anthropic.chrome") == "Action required"
    controller = SetupController(
        launch=Mock(return_value=True), verify=Mock(side_effect=OSError("boom"))
    )
    controller.start_all(["anthropic.edge"])
    controller.wait(5)
    assert controller.state("anthropic.edge") == "Action required"


def test_start_all_is_single_flight_with_itself_and_with_a_single_source_setup(monkeypatch):
    import threading

    _enabled(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    launches = []

    def launch(source_id):
        launches.append(source_id)
        entered.set()
        assert release.wait(5)
        return True

    controller = SetupController(launch=launch, verify=Mock(return_value=True))
    first = controller.start_all(["anthropic.chrome", "anthropic.edge"])
    assert first["active"] is True and first["total"] == 2
    assert entered.wait(5)
    running = controller.run_report()
    assert running["current"] == ["anthropic.chrome"]
    assert running["queued"] == ["anthropic.edge"]
    assert controller.state("anthropic.chrome") == "Connecting"
    # Neither another batch nor a single-source setup starts while one is active.
    controller.start_all(["anthropic.chromium"])
    assert controller.start("github.gh-cli") == "Connecting"
    release.set()
    controller.wait(5)
    assert launches == ["anthropic.chrome", "anthropic.edge"]


def test_cancel_stops_after_the_item_being_authorized_and_leaves_the_rest_untouched(monkeypatch):
    import threading

    _enabled(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def launch(source_id):
        entered.set()
        assert release.wait(5)
        return True

    launch_mock = Mock(side_effect=launch)
    controller = SetupController(launch=launch_mock, verify=Mock(return_value=True))
    controller.start_all(["anthropic.chrome", "anthropic.edge"])
    assert entered.wait(5)
    controller.cancel()
    assert controller.run_report()["cancelled"] is True
    assert controller.run_report()["queued"] == []
    release.set()
    controller.wait(5)
    assert launch_mock.call_count == 1
    assert controller.state("anthropic.chrome") == "Limited"
    assert controller.state("anthropic.edge") == "Setup required"
    run = controller.run_report()
    assert (run["active"], run["done"], run["total"], run["cancelled"]) == (False, 1, 2, True)


def test_a_source_turned_off_mid_run_is_skipped_and_never_verified(monkeypatch):
    import threading

    config = _enabled(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def launch(source_id):
        entered.set()
        assert release.wait(5)
        return True

    verify = Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    controller.start_all(["anthropic.chrome", "anthropic.edge"])
    assert entered.wait(5)
    # Edge is switched off while Chrome's prompt is up.
    consent.POLICY.configure(consent.SourcePolicy.change(config, "anthropic.edge", read=False))
    release.set()
    controller.wait(5)
    assert [c.args[0] for c in verify.call_args_list] == ["anthropic.chrome"]
    assert controller.state("anthropic.edge") == "Off"


def test_consent_withdrawn_during_launch_drops_the_result(monkeypatch):
    import threading

    config = _enabled(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def launch(source_id):
        entered.set()
        assert release.wait(5)
        return True

    verify = Mock(return_value=True)
    controller = SetupController(launch=launch, verify=verify)
    controller.start_all(["anthropic.chrome"])
    assert entered.wait(5)
    consent.POLICY.configure(consent.SourcePolicy.change(config, "anthropic.chrome", read=False))
    release.set()
    controller.wait(5)
    verify.assert_not_called()
    assert controller.state("anthropic.chrome") == "Off"


def test_an_exception_while_finishing_a_group_ends_the_run_and_does_not_stop_the_others(
    monkeypatch,
):
    _enabled(monkeypatch)
    controller = SetupController(launch=Mock(return_value=True), verify=Mock(return_value=True))
    real_finish = controller._finish

    def finish(source_id, ticket, success):
        if source_id == "anthropic.chrome":
            raise RuntimeError("boom")
        real_finish(source_id, ticket, success)

    controller._finish = finish  # type: ignore[method-assign]
    controller.start_all(["anthropic.chrome", "anthropic.edge"])
    controller.wait(5)
    # The run is over (the page must not sit on "Authorizing" forever) and cannot be left active.
    run = controller.run_report()
    assert (run["active"], run["done"], run["total"]) == (False, 2, 2)
    assert controller.state("anthropic.chrome") == "Action required"
    assert controller.state("anthropic.edge") == "Limited"
    # A new run can start afterwards.
    assert controller.start_all(["anthropic.chromium"])["total"] == 1
    controller.wait(5)


def test_a_failure_finishing_a_later_member_keeps_an_earlier_sibling_authorized(monkeypatch):
    from scripts.sidecar_pkg.source_catalog import catalog

    _enabled(monkeypatch)
    # Two sources that read the same Keychain item, so they run as one group.
    first, second = sorted(d.id for d in catalog().values() if d.browser_key == "chrome")[:2]
    controller = SetupController(launch=Mock(return_value=True), verify=Mock(return_value=True))
    real_finish = controller._finish

    def finish(source_id, ticket, success):
        if source_id == second:
            raise RuntimeError("boom")
        real_finish(source_id, ticket, success)

    controller._finish = finish  # type: ignore[method-assign]
    controller.start_all([first, second])
    controller.wait(5)
    assert controller.state(first) == "Limited"
    assert controller.state(second) == "Action required"
