"""opencode v2 keeps credentials in ``opencode.db`` (``credential`` table).

v2 imports ``auth.json`` once and never reads it again, leaving the file behind
stale. These tests pin that the sidecar serves the table through the legacy
rules (same mappings, same origin), gates account binding on it, and that the
xAI keep-alive never touches a superseded file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import scripts.sidecar as sc
from scripts.sidecar_pkg import opencode_credentials as oc
from scripts.sidecar_pkg.identity import credential_fingerprint
from scripts.sidecar_pkg.xai_renewer import XaiRenewer

KEY_NEW = "oc_sk_v2_table_key"  # pragma: allowlist secret
KEY_STALE = "oc_sk_stale_authjson_key"  # pragma: allowlist secret
KEY_OTHER = "oc_sk_other_account_key"  # pragma: allowlist secret


def _make_db(
    directory: Path,
    credentials: list[dict[str, Any]],
    *,
    v2: bool = True,
) -> Path:
    """An ``opencode.db`` with a ``credential`` table (and ``session_v2`` when v2)."""
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT NOT NULL, "
        "value TEXT NOT NULL, connector_id TEXT, method_id TEXT, active INTEGER, "
        "time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)"
    )
    if v2:
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY)")
    for i, c in enumerate(credentials):
        conn.execute(
            "INSERT INTO credential VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"cred_{i}",
                c["integration"],
                "API key",
                json.dumps(c["value"]),
                None,
                None,
                c.get("active"),
                1,
                c.get("updated", 1),
            ),
        )
    conn.commit()
    conn.close()
    return db


def _key(integration: str, key: str, **extra: Any) -> dict[str, Any]:
    return {"integration": integration, "value": {"type": "key", "key": key}, **extra}


def _auth_stale(directory: Path, key: str = KEY_STALE) -> Path:
    path = directory / "auth.json"
    path.write_text(json.dumps({"opencode-go": {"type": "api", "key": key}}), encoding="utf-8")
    return path


def _file_config(auth_path: Path) -> dict[str, Any]:
    return {
        "name": "OpenCode",
        "icon": "⚡",
        "rules": [
            {
                "type": "file",
                "paths": [str(auth_path)],
                "format": "json",
                "mapping": {"opencode-go.key": "api_key"},
            }
        ],
    }


@pytest.fixture
def oc_dir(tmp_path: Path) -> Path:
    # sibling_db() only trusts directories named like opencode's own data dir.
    d = tmp_path / ".local" / "share" / "opencode"
    d.mkdir(parents=True)
    return d


class TestLoader:
    def test_keys_and_oauth_keep_the_legacy_field_names(self, oc_dir: Path) -> None:
        oauth = {"type": "oauth", "methodID": "m", "refresh": "r1", "access": "a1", "expires": 5}
        db = _make_db(
            oc_dir,
            [_key("opencode-go", KEY_NEW), {"integration": "xai", "value": oauth}],
        )
        view = oc.load_auth_view(db)
        assert view is not None
        assert view["opencode-go"]["key"] == KEY_NEW
        assert (view["xai"]["access"], view["xai"]["refresh"], view["xai"]["expires"]) == (
            "a1",
            "r1",
            5,
        )

    def test_prefers_the_active_row(self, oc_dir: Path) -> None:
        db = _make_db(
            oc_dir,
            [
                _key("opencode-go", KEY_OTHER, updated=99),
                _key("opencode-go", KEY_NEW, active=1, updated=1),
            ],
        )
        assert oc.load_auth_view(db)["opencode-go"]["key"] == KEY_NEW  # type: ignore[index]

    def test_unreadable_db_returns_none(self, tmp_path: Path) -> None:
        junk = tmp_path / "opencode.db"
        junk.write_bytes(b"not sqlite")
        assert oc.load_auth_view(junk) is None

    def test_opens_read_only(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("opencode-go", KEY_NEW)])
        before = db.stat().st_mtime_ns
        oc.load_auth_view(db)
        oc.key_binding(db, "opencode-go", KEY_NEW)
        assert db.stat().st_mtime_ns == before
        assert not (oc_dir / "opencode.db-wal").exists()


class TestV2Detection:
    def test_v2_needs_session_v2_not_just_the_credential_table(self, oc_dir: Path) -> None:
        # opencode 1.19 already ships an (empty) credential table.
        _make_db(oc_dir, [], v2=False)
        assert oc.v2_db_for_auth(oc_dir / "auth.json") is None

    def test_v2_db_detected_even_when_auth_json_is_gone(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("opencode-go", KEY_NEW)])
        assert oc.v2_db_for_auth(oc_dir / "auth.json") == db

    def test_unrelated_auth_json_is_never_matched(self, tmp_path: Path) -> None:
        other = tmp_path / ".codex"
        other.mkdir()
        _make_db(other, [_key("opencode-go", KEY_NEW)])
        assert oc.v2_db_for_auth(other / "auth.json") is None


class TestKeyBinding:
    def test_single_when_discovered_key_is_the_active_credential(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("opencode-go", KEY_NEW)])
        assert oc.key_binding(db, "opencode-go", KEY_NEW) == "single"

    def test_ambiguous_when_the_active_credential_differs(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("opencode-go", KEY_NEW, active=1)])
        assert oc.key_binding(db, "opencode-go", KEY_OTHER) == "ambiguous"

    def test_ambiguous_when_several_unflagged_credentials_compete(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("opencode-go", KEY_NEW), _key("opencode-go", KEY_OTHER)])
        assert oc.key_binding(db, "opencode-go", KEY_NEW) == "ambiguous"

    def test_unknown_without_a_credential_for_the_service(self, oc_dir: Path) -> None:
        db = _make_db(oc_dir, [_key("openrouter", KEY_NEW)])
        assert oc.key_binding(db, "opencode-go", KEY_NEW) == "unknown"


class TestSidecarFileRule:
    def test_reads_the_table_not_the_stale_auth_json(
        self, oc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [])
        auth = _auth_stale(oc_dir)
        _make_db(oc_dir, [_key("opencode-go", KEY_NEW)])

        _, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        # Origin keeps the legacy path form (so existing tag hints still bind) but the
        # fingerprint is of the table's key, proving the file was not the source.
        assert blocked == [
            {
                "provider_id": "opencode",
                "credential_origin": f"path:{auth.resolve()}#{credential_fingerprint(KEY_NEW)}",
            }
        ]

    def test_fresh_v2_install_with_no_auth_json_still_discovers(
        self, oc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [])
        _make_db(oc_dir, [_key("opencode-go", KEY_NEW)])
        auth = oc_dir / "auth.json"  # never written by v2
        assert not auth.exists()

        _, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        assert [b["credential_origin"].rsplit("#", 1)[1] for b in blocked] == [
            credential_fingerprint(KEY_NEW)
        ]

    def test_v1_install_still_reads_auth_json(
        self, oc_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [])
        auth = _auth_stale(oc_dir, KEY_STALE)
        _make_db(oc_dir, [_key("opencode-go", KEY_NEW)], v2=False)

        _, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        assert blocked[0]["credential_origin"].endswith(credential_fingerprint(KEY_STALE))

    def test_binding_gate_uses_the_table_on_v2(self, oc_dir: Path) -> None:
        auth = _auth_stale(oc_dir)
        _make_db(oc_dir, [_key("opencode-go", KEY_NEW, active=1)])
        origin = f"path:{auth.resolve()}#{credential_fingerprint(KEY_NEW)}"
        assert sc._opencode_local_key_binding(KEY_NEW, auth_origin=origin) == "single"
        assert sc._opencode_local_key_binding(KEY_STALE, auth_origin=origin) == "ambiguous"


class TestXaiRenewerReadOnlyOnV2:
    def _targets(self, monkeypatch: pytest.MonkeyPatch, oc_dir: Path, tmp_path: Path):
        from scripts.sidecar_pkg import xai_renewer

        monkeypatch.setattr(xai_renewer, "OPENCODE_PATHS", (oc_dir / "auth.json",))
        monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
        return XaiRenewer()._targets()

    def test_superseded_opencode_login_is_not_a_target(
        self, oc_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (oc_dir / "auth.json").write_text(
            json.dumps({"xai": {"access": "a", "refresh": "r", "expires": 1}}), encoding="utf-8"
        )
        _make_db(oc_dir, [])
        targets = self._targets(monkeypatch, oc_dir, tmp_path)
        assert [kind for _, kind in targets] == ["grok"]

    def test_v1_opencode_login_is_still_renewed(
        self, oc_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_db(oc_dir, [], v2=False)
        targets = self._targets(monkeypatch, oc_dir, tmp_path)
        assert "opencode" in {kind for _, kind in targets}

    def test_superseded_file_is_never_written(
        self, oc_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth = oc_dir / "auth.json"
        auth.write_text(
            json.dumps({"xai": {"access": "a", "refresh": "r", "expires": 1}}), encoding="utf-8"
        )
        _make_db(oc_dir, [])
        before = auth.read_bytes()
        from scripts.sidecar_pkg import xai_renewer

        monkeypatch.setattr(xai_renewer, "OPENCODE_PATHS", (auth,))
        monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))

        def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise AssertionError("must not refresh a login opencode v2 owns")

        monkeypatch.setattr(xai_renewer, "request_refresh", _boom)
        renewer = XaiRenewer()
        assert renewer.due() is False
        assert renewer.renew() is True
        assert auth.read_bytes() == before
