"""Fleet directory additions feed every existing discovery path without owning config."""

import json
from unittest.mock import MagicMock

import pytest

from app.core.login_dirs import clean_login_dirs
from scripts import sidecar


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("RUNWAY_API_URL", raising=False)
    monkeypatch.delenv("RUNWAY_API_KEY", raising=False)
    monkeypatch.setattr(sidecar, "_LOGIN_DIR_REMOTE", clean_login_dirs({}))
    monkeypatch.setattr(sidecar, "_LOGIN_DIR_EXTRAS", {})
    monkeypatch.setattr(sidecar, "_LOGIN_DIR_SERVER", None)


def test_remote_roots_drive_credentials_metadata_logs_and_renewal(tmp_path, monkeypatch):
    root = tmp_path / "extra"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    (root / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "example"}})
    )
    (root / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "work@example.com"}})
    )
    (root / "auth.json").write_text("{}")
    (root / "projects" / "a.jsonl").write_text("")
    (root / "sessions" / "b.jsonl").write_text("")
    dirs = {"claude_config_dirs": [str(root)], "codex_home": [str(root)]}
    sidecar.apply_remote_login_dirs(dirs)
    assert root / ".credentials.json" in sidecar._claude_login_paths()
    assert root / "auth.json" in sidecar._codex_login_paths()
    assert sidecar.discover_anthropic_oauth_email(root / ".credentials.json") == "work@example.com"
    assert root / "projects" / "a.jsonl" in sidecar._discover_anthropic_log_paths()
    assert root / "sessions" / "b.jsonl" in sidecar._discover_codex_log_paths()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    sidecar.configure_login_dirs({"claude_config_dirs": [str(root)]})
    assert sidecar.custom_login_dirs("CLAUDE_CONFIG_DIR") == [root]
    assert sidecar.login_directory_report()["applied"] == dirs
    sidecar.apply_remote_login_dirs(clean_login_dirs({}))
    assert root in sidecar.custom_login_dirs("CLAUDE_CONFIG_DIR")  # local/env survive clearing


def test_reload_failure_and_pairing_scope(tmp_path):
    config = {"api_url": "https://one.example", "api_key": "one"}  # pragma: allowlist secret
    sidecar.configure_login_dirs(config)
    sidecar.apply_remote_login_dirs({"claude_config_dirs": [str(tmp_path / "missing")]})
    sidecar.configure_login_dirs(config)
    assert str(tmp_path / "missing") in sidecar.login_directory_report()["searched"]["anthropic"]
    assert not sidecar.apply_remote_login_dirs({"claude_config_dirs": "invalid"})
    assert sidecar._LOGIN_DIR_REMOTE["claude_config_dirs"]
    sidecar.configure_login_dirs({**config, "api_key": "different"})  # pragma: allowlist secret
    assert sidecar._LOGIN_DIR_REMOTE == clean_login_dirs({})


def test_response_applies_without_starting_tray_keepalive(tmp_path, monkeypatch):
    controller = MagicMock()
    monkeypatch.setattr(sidecar, "_KEEP_ALIVE", controller)
    runner = sidecar.DaemonRunner(
        {"api_url": "https://example.com", "api_key": "key"}  # pragma: allowlist secret
    )
    runner._apply_ingest_instructions(
        {"login_dirs": {"codex_home": [str(tmp_path)]}}, [], False, False, False
    )
    assert sidecar.custom_login_dirs("CODEX_HOME") == [tmp_path]
    controller.arm.assert_not_called()
    runner._apply_ingest_instructions({}, [], False, False, False)
    assert sidecar.custom_login_dirs("CODEX_HOME") == [
        tmp_path
    ]  # older server / missing instruction


def test_existing_file_cannot_be_used_as_root(tmp_path, caplog):
    file = tmp_path / "arbitrary"
    file.write_text("")
    sidecar.apply_remote_login_dirs({"codex_home": [str(file)]})
    with caplog.at_level("DEBUG"):
        assert sidecar.custom_login_dirs("CODEX_HOME") == []
    assert "Fleet login directory is not a directory on this host" in caplog.text


def test_old_response_cannot_restore_directories_after_pairing_switch(tmp_path):
    old = {
        "api_url": "https://old.example",
        "api_key": "old",  # pragma: allowlist secret
    }
    runner = sidecar.DaemonRunner(old)
    sidecar.configure_login_dirs(old)
    sidecar.apply_remote_login_dirs({"codex_home": [str(tmp_path / "old")]})
    new = {
        "api_url": "https://new.example",
        "api_key": "new",  # pragma: allowlist secret
    }
    runner._config = new
    sidecar.configure_login_dirs(new)
    runner._apply_ingest_instructions(
        {"login_dirs": {"codex_home": [str(tmp_path / "old")]}},
        [],
        False,
        False,
        False,
        request_target=(old["api_url"], old["api_key"]),
    )
    sidecar.scope_login_dirs(new)
    assert sidecar._LOGIN_DIR_REMOTE == clean_login_dirs({})


def test_personal_and_work_logs_keep_their_own_accounts(tmp_path):
    import datetime

    from scripts.sidecar_pkg.event_watermark import EventWatermark

    personal = tmp_path / ".claude"
    work = tmp_path / "work"
    for root, email, message in (
        (personal, "personal@example.com", "personal-msg"),
        (work, "work@example.com", "work-msg"),
    ):
        (root / "projects").mkdir(parents=True)
        metadata = tmp_path / ".claude.json" if root == personal else root / ".claude.json"
        metadata.write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))
        (root / "projects" / "session.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    "message": {
                        "id": message,
                        "model": "claude-sonnet-4",
                        "usage": {"input_tokens": 7},
                    },
                }
            )
            + "\n"
        )
    sidecar.apply_remote_login_dirs({"claude_config_dirs": [str(work)]})
    events, blocked = [], []
    assert sidecar._extract_directory_events(
        "anthropic", EventWatermark(tmp_path / "watermarks"), 90, {}, events, blocked
    ) == (0, True)
    assert {(e["event_id"], e["account_id"]) for e in events} == {
        ("personal-msg", "personal@example.com"),
        ("work-msg", "work@example.com"),
    }
    assert blocked == []


def test_codex_roots_keep_distinct_accounts_and_per_account_watermarks(tmp_path):
    import base64
    import datetime

    from scripts.sidecar_pkg.event_watermark import EventWatermark

    roots = [tmp_path / ".codex", tmp_path / "work-codex"]
    for index, root in enumerate(roots):
        (root / "sessions").mkdir(parents=True)
        payload = (
            base64.urlsafe_b64encode(json.dumps({"email": f"user{index}@example.com"}).encode())
            .decode()
            .rstrip("=")
        )
        (root / "auth.json").write_text(
            json.dumps({"tokens": {"id_token": f"header.{payload}.signature"}})
        )
        sample = (
            __import__("pathlib").Path(__file__).parents[1] / "fixtures" / "chatgpt-sample.jsonl"
        ).read_text()
        (root / "sessions" / "test.jsonl").write_text(sample)
    sidecar.apply_remote_login_dirs({"codex_home": [str(roots[1])]})
    wm = EventWatermark(tmp_path / "wm")
    events = []
    assert sidecar._extract_directory_events("chatgpt", wm, 1000, {}, events, []) == (0, True)
    assert {e["account_id"] for e in events} == {"user0@example.com", "user1@example.com"}
    wm.advance("chatgpt", "user0@example.com", datetime.datetime.now(datetime.UTC))
    events = []
    sidecar._extract_directory_events("chatgpt", wm, 1000, {}, events, [])
    assert {e["account_id"] for e in events} == {"user1@example.com"}


def test_unknown_root_uses_its_own_hint_not_another_roots_identity(tmp_path):
    import datetime

    from scripts.sidecar_pkg.event_watermark import EventWatermark

    root = tmp_path / "unknown"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "a.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "message": {"id": "unknown-msg", "usage": {"input_tokens": 1}},
            }
        )
    )
    sidecar.apply_remote_login_dirs({"claude_config_dirs": [str(root)]})
    events, blocked = [], []
    hint = {"anthropic": {f"path:{root}/projects": "assigned@example.com"}}
    sidecar._extract_directory_events(
        "anthropic", EventWatermark(tmp_path / "wm"), 90, hint, events, blocked
    )
    assert events[0]["account_id"] == "assigned@example.com"
    assert events[0]["account_source"] == "tag"
    assert blocked == []


def test_idle_unresolved_log_root_does_not_clear_pending_manifest(tmp_path):
    import datetime

    from scripts.sidecar_pkg.event_watermark import EventWatermark

    root = tmp_path / "unknown"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "a.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                "message": {"id": "idle-msg", "usage": {"input_tokens": 1}},
            }
        )
    )
    sidecar.apply_remote_login_dirs({"claude_config_dirs": [str(root)]})
    watermark = EventWatermark(tmp_path / "watermark")
    events, blocked = [], []
    assert sidecar._extract_directory_events("anthropic", watermark, 90, {}, events, blocked) == (
        0,
        True,
    )
    assert blocked[0]["credential_origin"] == f"path:{root}/projects"
    watermark.advance("anthropic", "default", datetime.datetime.now(datetime.UTC))
    assert sidecar._extract_directory_events("anthropic", watermark, 90, {}, [], []) == (0, False)
    (root / ".credentials.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "inline@example.com"}})
    )
    events = []
    assert sidecar._extract_directory_events("anthropic", watermark, 90, {}, events, []) == (
        0,
        True,
    )
    assert events[0]["account_id"] == "inline@example.com"


def test_partial_batch_failure_cannot_apply_previous_pairing_response(tmp_path, monkeypatch):
    old = {
        "api_url": "https://old.example",
        "api_key": "old",  # pragma: allowlist secret
    }
    new = {
        "api_url": "https://new.example",
        "api_key": "new",  # pragma: allowlist secret
    }
    runner = sidecar.DaemonRunner(old)
    monkeypatch.setattr(
        sidecar,
        "run_collection",
        lambda *_args, **_kwargs: sidecar.CollectionResult(
            [], [{"event_id": str(i)} for i in range(1001)], 0, []
        ),
    )
    monkeypatch.setattr(sidecar, "queue_flush", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "queue_push", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sidecar, "_tail_log", lambda *_args: [])
    calls = 0

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True, {"login_dirs": {"codex_home": [str(tmp_path / "old")]}}, 200
        runner._config = new
        sidecar.configure_login_dirs(new)
        return False, {}, 500

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", post)
    assert runner.run_once() is False
    assert sidecar._LOGIN_DIR_REMOTE == clean_login_dirs({})


def test_new_columns_have_forward_safe_migrations():
    from app.core.db import _DEFERRED_COLUMNS

    assert ("sidecar_registry", "login_dirs_desired", "TEXT") in _DEFERRED_COLUMNS
    assert ("sidecar_registry", "login_dirs_report", "TEXT") in _DEFERRED_COLUMNS
    assert ("usage_events", "credential_origin", "VARCHAR") in _DEFERRED_COLUMNS
