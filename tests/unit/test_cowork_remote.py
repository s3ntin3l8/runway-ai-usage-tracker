"""The sidecar reports remote Cowork session ids from Claude Desktop, and nothing else from the file."""

import json

from scripts.sidecar_pkg.cowork_remote import MAX_HINTS, collect_remote_session_hints

ACCT = "00000000-0000-4000-8000-00000000a0c1"
ORG = "00000000-0000-4000-8000-0000000000f9"
A = "01Qx7pLm3RtV9wKe2NbYh4Ds"  # pragma: allowlist secret
B = "01Hn4TzW8cJq2XvPa6LmRe9K"  # pragma: allowlist secret


def write(root, entries, acct=ACCT, org=ORG):
    folder = root / acct / org
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "remote-session-spaces.json"
    path.write_text(json.dumps({"entries": entries}))
    return path


def test_every_entry_becomes_a_hint_with_the_api_id_and_no_folders(tmp_path):
    write(
        tmp_path,
        [
            {
                "sessionId": f"session_{A}",
                "spaceId": "3f2a9c1e-7b4d-4e8a-9c2f-1d6e5a8b0c7f",
                "folders": ["/fake/path"],
                "memoryEnabled": True,
            },
            {"sessionId": f"session_{B}", "folders": ["/fake/path", "/fake/other"]},
        ],
    )
    hints = collect_remote_session_hints(tmp_path)
    assert hints == [
        {"session_id": f"cse_{A}", "account_uuid": ACCT, "org_uuid": ORG},
        {"session_id": f"cse_{B}", "account_uuid": ACCT, "org_uuid": ORG},
    ]
    assert "/fake" not in json.dumps(hints)


def test_malformed_ids_and_entries_are_skipped(tmp_path):
    write(
        tmp_path,
        [
            {"sessionId": "session_tooShort"},
            {"sessionId": f"cse_{A}"},  # not the Desktop form
            {"sessionId": f"session_{A}x"},
            {"sessionId": 12},
            "not an entry",
            {"folders": ["/fake/path"]},
            {"sessionId": f"session_{B}"},
            {"sessionId": f"session_{B}"},  # duplicate
        ],
    )
    assert [h["session_id"] for h in collect_remote_session_hints(tmp_path)] == [f"cse_{B}"]


def test_directories_that_are_not_uuids_are_skipped(tmp_path):
    write(tmp_path, [{"sessionId": f"session_{A}"}], acct="not-a-uuid")
    write(tmp_path, [{"sessionId": f"session_{B}"}], org="also not")
    assert collect_remote_session_hints(tmp_path) == []


def test_missing_unparsable_and_odd_files_send_nothing(tmp_path):
    assert collect_remote_session_hints(tmp_path / "absent") == []
    path = write(tmp_path, [])
    path.write_text("{ not json")
    assert collect_remote_session_hints(tmp_path) == []
    path.write_text(json.dumps({"entries": {"sessionId": f"session_{A}"}}))
    assert collect_remote_session_hints(tmp_path) == []
    path.write_text(json.dumps([{"sessionId": f"session_{A}"}]))
    assert collect_remote_session_hints(tmp_path) == []


def test_the_hint_count_is_bounded(tmp_path):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ids = [f"session_01{alphabet[i % 62]}{alphabet[i // 62]}{'x' * 20}" for i in range(600)]
    write(tmp_path, [{"sessionId": sid} for sid in ids])
    assert len(collect_remote_session_hints(tmp_path)) == MAX_HINTS


def _stub_collection(sidecar, monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    monkeypatch.setattr(sidecar, "_extract_events_for_provider", lambda **kw: 0)
    monkeypatch.setattr(sidecar, "_post_credential_manifest", lambda **kw: None)
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda *a: [])


def test_a_mac_collection_cycle_carries_the_hints(tmp_path, monkeypatch):
    from scripts import sidecar

    _stub_collection(sidecar, monkeypatch, tmp_path)
    write(
        tmp_path / "Library/Application Support/Claude/local-agent-mode-sessions",
        [{"sessionId": f"session_{A}", "folders": ["/fake/path"]}],
    )
    result = sidecar.run_collection({"providers": ["anthropic"]}, ["anthropic"])
    assert result.remote_sessions == [
        {"session_id": f"cse_{A}", "account_uuid": ACCT, "org_uuid": ORG}
    ]
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Linux")
    assert sidecar.run_collection({"providers": ["anthropic"]}, ["anthropic"]).remote_sessions == []


def test_the_hints_go_out_with_the_first_batch_only(tmp_path, monkeypatch):
    from scripts import sidecar

    hint = {"session_id": f"cse_{A}", "account_uuid": ACCT, "org_uuid": ORG}
    two = [
        {
            "provider_id": "anthropic",
            "account_id": "a@example.com",
            "event_id": f"e{i}",
            "ts": "2026-11-25T10:00:00+00:00",
        }
        for i in range(1001)  # one more than a batch holds
    ]
    result = sidecar.CollectionResult([], two, 0, [], remote_sessions=[hint])
    monkeypatch.setattr(sidecar, "run_collection", lambda *a, **kw: result)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway"))
    monkeypatch.setattr(sidecar, "_tail_log", lambda n: [])
    monkeypatch.setattr(sidecar.DaemonRunner, "_apply_ingest_instructions", lambda *a, **kw: None)
    sent = []

    def post(url, payload, *a, **kw):
        sent.append(payload)
        return True, {}, 200

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", post)
    config = {
        "api_url": "http://localhost:9999",
        "api_key": "synthetic",  # pragma: allowlist secret
    }
    sidecar.DaemonRunner(config).run_once(providers=["anthropic"])
    ingest = [p for p in sent if isinstance(p, dict) and "events" in p]
    assert len(ingest) == 2
    assert ingest[0]["claude_remote_sessions"] == [hint]
    assert ingest[1]["claude_remote_sessions"] is None
