"""Native credential discovery stays read-only, isolated and account scoped."""

import base64
import hashlib
import json
import subprocess

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ACCOUNT = "11111111-1111-4111-8111-111111111111"
SLOT = f"acct:{ACCOUNT}|client:org:api.anthropic.com:user:profile user:inference"


def encrypted(value):
    key = hashlib.pbkdf2_hmac("sha1", b"synthetic-key", b"saltysalt", 1003, 16)
    pad = padding.PKCS7(128).padder()
    raw = json.dumps(value).encode()
    padded = pad.update(raw) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return base64.b64encode(b"v10" + enc.update(padded) + enc.finalize()).decode()


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import macos_cookies

    monkeypatch.setattr(macos_cookies, "_KEY_CACHE", {})
    calls = []

    def lookup(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "synthetic-key\n", "")

    monkeypatch.setattr(subprocess, "run", lookup)
    path = tmp_path / "config.json"
    return path, calls


def test_desktop_deduplicates_v2_and_never_forwards_refresh(desktop):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, calls = desktop
    entry = {"token": "test-access", "refreshToken": "never-forward", "expiresAt": 4102444800000}
    path.write_text(
        json.dumps(
            {k: encrypted({SLOT: entry}) for k in ("oauth:tokenCache", "oauth:tokenCacheV2")}
        )
    )
    result = read_oauth_candidates(path)
    assert len(result) == 1
    tokens, origin = result[0]
    assert tokens == {
        "oauth_token": "test-access",
        "expiry_date": "4102444800000",
        "oauth_account_uuid": ACCOUNT,
        "oauth_read_only": "1",
    }
    assert origin.startswith("keychain:Claude Desktop/oauth/")
    assert "never-forward" not in repr(result)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 30


def test_desktop_rereads_rotation_without_repeating_keychain(desktop):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, calls = desktop
    results = []
    for token in ("first", "second"):
        path.write_text(
            json.dumps(
                {
                    "oauth:tokenCacheV2": encrypted(
                        {SLOT: {"token": token, "expiresAt": 4102444800000}}
                    )
                }
            )
        )
        results.append(read_oauth_candidates(path)[0])
    assert [r[0]["oauth_token"] for r in results] == ["first", "second"]
    assert results[0][1] == results[1][1]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "entry",
    [
        None,
        [],
        {"refreshToken": "secret"},
        {"token": 42},
        {"token": "access", "expiresAt": "invalid"},
    ],
)
def test_desktop_unsupported_entries_are_not_credentials(desktop, entry):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, _ = desktop
    path.write_text(json.dumps({"oauth:tokenCacheV2": encrypted({SLOT: entry})}))
    assert read_oauth_candidates(path) == []


def test_desktop_opt_out_never_accesses_store(desktop, monkeypatch):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, calls = desktop
    monkeypatch.setenv("RUNWAY_CLAUDE_DESKTOP_CREDENTIALS", "0")
    path.write_text("not-json")
    assert read_oauth_candidates(path) == []
    assert calls == []


def test_v2_removed_slot_cannot_fall_back_to_legacy_token(desktop):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, _ = desktop
    path.write_text(
        json.dumps(
            {
                "oauth:tokenCacheV2": encrypted({SLOT: None}),
                "oauth:tokenCache": encrypted(
                    {SLOT: {"token": "stale-legacy", "expiresAt": 4102444800000}}
                ),
            }
        )
    )
    assert read_oauth_candidates(path) == []


def test_distinct_desktop_account_slots_remain_independent(desktop):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, _ = desktop
    other = SLOT.replace(ACCOUNT, "22222222-2222-4222-8222-222222222222")
    path.write_text(
        json.dumps(
            {
                "oauth:tokenCacheV2": encrypted(
                    {
                        SLOT: {"token": "first", "expiresAt": 1},
                        other: {"token": "second", "expiresAt": 4102444800000},
                    }
                )
            }
        )
    )
    result = read_oauth_candidates(path)
    assert len(result) == 2
    assert result[0][1] != result[1][1]
    assert {r[0]["oauth_account_uuid"] for r in result} == {
        ACCOUNT,
        "22222222-2222-4222-8222-222222222222",
    }


@pytest.mark.parametrize(
    "blob", ["invalid-base64!", base64.b64encode(b"v20unsupported").decode(), encrypted([])]
)
def test_desktop_bad_encryption_does_not_leak_data(desktop, blob, caplog):
    from scripts.sidecar_pkg.claude_desktop import read_oauth_candidates

    path, _ = desktop
    path.write_text(json.dumps({"oauth:tokenCacheV2": blob}))
    assert read_oauth_candidates(path) == []
    assert blob not in caplog.text


def test_production_rule_emits_pending_oauth_without_inheriting_other_account(desktop, monkeypatch):
    from scripts import sidecar

    path, _ = desktop
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.Path, "home", lambda: path.parent)
    actual = path.parent / "Library/Application Support/Claude/config.json"
    actual.parent.mkdir(parents=True)
    actual.write_text(
        json.dumps(
            {
                "oauth:tokenCacheV2": encrypted(
                    {SLOT: {"token": "access", "expiresAt": 4102444800000}}
                )
            }
        )
    )
    cards, blocked = sidecar.GenericCollector.collect_provider(
        "anthropic",
        {"rules": [{"type": "claude_desktop_oauth"}]},
        account_label_hints={"anthropic": {"provider:anthropic": "wrong@example.com"}},
    )
    assert len(cards) == len(blocked) == 1
    assert cards[0]["metadata"]["identity_pending"] is True
    assert cards[0]["account_id"] is None
    assert cards[0]["metadata"]["oauth_account_uuid"] == ACCOUNT


def test_code_services_separate_explicit_default_and_override(tmp_path, monkeypatch):
    from scripts.sidecar_pkg.claude_code_keychain import service_candidates

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    assert service_candidates([]) == ["Claude Code-credentials"]
    root = tmp_path / "work"
    expected = "Claude Code-credentials-" + hashlib.sha256(str(root).encode()).hexdigest()[:8]
    assert service_candidates([root]) == ["Claude Code-credentials", expected]
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    assert service_candidates([root]) == ["Claude Code-credentials"]


def test_code_denial_is_not_retried_and_errors_are_sanitized(monkeypatch, caplog):
    from scripts import sidecar  # noqa: F401 - initialize before subprocess isolation
    from scripts.sidecar_pkg import claude_code_keychain as helper

    monkeypatch.setattr(helper, "_DENIED", set())
    calls = []

    def deny(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 30, output="secret-output")

    monkeypatch.setattr(subprocess, "run", deny)
    for _ in range(2):
        assert helper.read_candidates([], {"claudeAiOauth.accessToken": "oauth_token"}) == []
    assert len(calls) == 1
    assert "-a" in calls[0]
    assert "secret-output" not in caplog.text


def test_code_reads_rotated_values_and_expiry_without_exporting_refresh(monkeypatch):
    from scripts.sidecar_pkg import claude_code_keychain as helper

    monkeypatch.setattr(helper, "_DENIED", set())
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    values = iter(["first", "second"])

    def lookup(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": next(values),
                        "refreshToken": "private",
                        "expiresAt": 4102444800000,
                    }
                }
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", lookup)
    mapping = {
        "claudeAiOauth.accessToken": "oauth_token",
        "claudeAiOauth.refreshToken": "refresh_token",
        "claudeAiOauth.expiresAt": "expiry_date",
    }
    results = [helper.read_candidates([], mapping)[0] for _ in range(2)]
    assert [r[0]["oauth_token"] for r in results] == ["first", "second"]
    assert all(
        r[0]["expiry_date"] == "4102444800000" and "refresh_token" not in r[0] for r in results
    )


def test_code_denial_suppresses_all_service_prompts_until_restart(monkeypatch, tmp_path):
    from scripts import sidecar  # noqa: F401
    from scripts.sidecar_pkg import claude_code_keychain as helper

    monkeypatch.setattr(helper, "_DENIED", set())
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    calls = []

    def denied(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 36, "", "private-error")

    monkeypatch.setattr(subprocess, "run", denied)
    for _ in range(2):
        assert helper.read_candidates([tmp_path / "a", tmp_path / "b"], {}) == []
    assert len(calls) == 1


def test_code_multiple_directories_keep_account_tokens_separate(monkeypatch, tmp_path):
    from scripts import sidecar  # noqa: F401
    from scripts.sidecar_pkg import claude_code_keychain as helper

    monkeypatch.setattr(helper, "_DENIED", set())
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    roots = [tmp_path / "personal", tmp_path / "work"]
    services = helper.service_candidates(roots)

    def lookup(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == services[0]:
            return subprocess.CompletedProcess(cmd, 44, "", "")
        account = "personal@example.com" if service == services[1] else "work@example.com"
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {"claudeAiOauth": {"accessToken": account}, "oauthAccount": {"email": account}}
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", lookup)
    result = helper.read_candidates(
        roots, {"claudeAiOauth.accessToken": "oauth_token", "oauthAccount.email": "account_id"}
    )
    assert len(result) == 2 and result[0][1] != result[1][1]
    assert [(tokens["oauth_token"], tokens["account_id"]) for tokens, _ in result] == [
        ("personal@example.com", "personal@example.com"),
        ("work@example.com", "work@example.com"),
    ]


def test_code_secure_storage_override_uses_only_its_unicode_normalized_service(
    monkeypatch, tmp_path
):
    import unicodedata

    from scripts.sidecar_pkg.claude_code_keychain import service_candidates

    override = str(tmp_path / "cafe\u0301")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", override)
    expected = (
        "Claude Code-credentials-"
        + hashlib.sha256(unicodedata.normalize("NFC", override).encode()).hexdigest()[:8]
    )
    assert service_candidates([tmp_path / "unrelated"]) == [expected]
