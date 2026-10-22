"""Exact Cowork usage from synthetic, content-free Desktop sessions."""

import json
import os

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def record(mid="msg-a", ts="2026-07-01T12:00:00Z"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "cli-one",
        "entrypoint": "sdk-ts",
        "version": "2.1.280",
        "requestId": "req-a",
        "message": {
            "id": mid,
            "model": "claude-sonnet-4-5-20250929",
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_input_tokens": 23,
                "cache_creation_input_tokens": 13,
                "cache_creation": {"ephemeral_1h_input_tokens": 5, "ephemeral_5m_input_tokens": 8},
            },
        },
    }


def session(base, name="local_one", email="Alice@example.com", org="org-one"):
    root = base / "account-one" / org
    root.mkdir(parents=True, exist_ok=True)
    (root / (name + ".json")).write_text(
        json.dumps({"sessionId": name, "cliSessionId": "cli-one", "emailAddress": email})
    )
    path = root / name / ".claude" / "projects" / "project" / "cli-one.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record()) + "\n")
    return path


def collect(base, state, **kwargs):
    from scripts.sidecar_pkg.cowork_events import collect_cowork

    return collect_cowork(base, state, {}, **kwargs)


def test_exact_history_uses_existing_ids_and_no_raw_content(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    with path.open("a") as f:
        f.write(json.dumps(record()) + "\n")  # repeated content blocks
    (path.parents[3] / "audit.jsonl").write_text(json.dumps(record("audit-only")) + "\n")
    batch = collect(base, tmp_path / "state.json")
    assert len(batch.events) == 1
    ev = batch.events[0]
    assert ev["event_id"] == "msg-a"  # preserve legacy CLI identity
    assert ev["account_id"] == "alice@example.com"
    assert ev["ts"] == "2026-07-01T12:00:00+00:00"
    assert (
        ev["tokens_input"],
        ev["tokens_output"],
        ev["tokens_cache_read"],
        ev["tokens_cache_create"],
        ev["tokens_cache_create_1h"],
        ev["tokens_cache_create_5m"],
    ) == (11, 7, 23, 13, 5, 8)
    assert ev["session_id"] == "cli-one"
    assert ev["entrypoint"] == "sdk-ts"
    assert ev["app_version"] == "2.1.280"
    assert ev["raw_json"] is None
    assert ev["credential_origin"] == f"path:{base / 'account-one' / 'org-one'}"


def test_progress_requires_ownership_and_survives_restart(tmp_path):
    base = tmp_path / "desktop"
    session(base)
    state = tmp_path / "state.json"
    first = collect(base, state)
    assert len(collect(base, state).events) == 1  # failed/unqueued batch
    first.mark_owned([])
    assert len(collect(base, state).events) == 1
    first.mark_owned(first.events)
    assert collect(base, state).events == []


def test_partial_line_and_bounded_backfill(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    path.write_text(json.dumps(record()) + "\n" + json.dumps(record("msg-b")))
    state = tmp_path / "state.json"
    batch = collect(base, state, max_events=1)
    assert [x["event_id"] for x in batch.events] == ["msg-a"]
    batch.mark_owned(batch.events)
    assert collect(base, state).events == []
    with path.open("a") as f:
        f.write("\n")
    batch = collect(base, state)
    assert [x["event_id"] for x in batch.events] == ["msg-b"]


@pytest.mark.parametrize("change", ["truncate", "replace", "corrupt-state"])
def test_replaced_truncated_or_unreadable_state_replays_safely(tmp_path, change):
    base = tmp_path / "desktop"
    path = session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    if change == "corrupt-state":
        state.write_text("not json")
    elif change == "replace":
        path.unlink()
        path.write_text(json.dumps(record("msg-new")) + "\n")
    else:
        path.write_text(json.dumps(record("x")) + "\n")
    assert len(collect(base, state).events) == 1


def test_missing_identity_stays_pending_without_cli_borrowing(tmp_path):
    base = tmp_path / "desktop"
    session(base, email=None)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    assert batch.events[0]["account_id"] == "default"
    assert batch.events[0]["account_source"] == "default"
    assert batch.blocked[0]["reason"] == "events_untagged"


def test_conflicting_session_metadata_is_quarantined(tmp_path):
    base = tmp_path / "desktop"
    session(base)
    session(base, name="local_two", email="bob@example.com")
    batch = collect(base, tmp_path / "state.json")
    assert batch.events == []
    assert batch.blocked


def test_orgs_keep_their_own_identity_and_origins(tmp_path):
    base = tmp_path / "desktop"
    session(base)
    other = session(base, org="org-two", email="bob@example.com")
    other.write_text(json.dumps(record("msg-b")) + "\n")
    batch = collect(base, tmp_path / "state.json")
    assert {e["account_id"] for e in batch.events} == {"alice@example.com", "bob@example.com"}
    assert len({e["credential_origin"] for e in batch.events}) == 2


def test_existing_cli_ids_deduplicate_without_consuming_unowned_progress(tmp_path):
    base = tmp_path / "desktop"
    session(base)
    from scripts.sidecar_pkg.cowork_events import collect_cowork

    batch = collect_cowork(base, tmp_path / "state.json", {}, existing_ids={"msg-a"})
    assert batch.events == []
    batch.mark_owned([])
    assert len(collect(base, tmp_path / "state.json").events) == 1


def test_no_symlink_escape_or_unassociated_files(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    external = tmp_path / "private.jsonl"
    path.rename(external)
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("symlink unavailable")
    assert collect(base, tmp_path / "state.json").events == []


def test_bad_lines_do_not_block_following_usage(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    path.write_text("not json\n" + json.dumps(record()) + "\n")
    assert len(collect(base, tmp_path / "state.json").events) == 1


@pytest.mark.parametrize(
    "outcome,owned",
    [("accepted", True), ("events-error", False), ("queued", True), ("full", False)],
)
def test_daemon_commits_only_accepted_or_durably_queued_ranges(
    tmp_path, monkeypatch, outcome, owned
):
    from scripts import sidecar

    base = tmp_path / "desktop"
    path = session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    result = sidecar.CollectionResult([], batch.events, 0, [])
    result.cowork_batch = batch
    monkeypatch.setattr(sidecar, "run_collection", lambda *a, **kw: result)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway"))
    monkeypatch.setattr(sidecar, "_tail_log", lambda n: [])
    monkeypatch.setattr(sidecar.DaemonRunner, "_apply_ingest_instructions", lambda *a, **kw: None)
    response = (
        (True, {"events_error": outcome == "events-error"}, 200)
        if outcome in {"accepted", "events-error"}
        else (False, {"detail": "offline"}, 0)
    )
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **kw: response)
    config = {
        "api_url": "http://localhost:9999",
        "api_key": "synthetic",  # pragma: allowlist secret
        "queue_max_size_mb": 0.00001 if outcome == "full" else 10,
    }
    runner = sidecar.DaemonRunner(config)
    runner.run_once(providers=["anthropic"])
    assert bool(collect(base, state).events) is not owned
    if outcome == "queued":
        manifest = path.parents[4] / "local_one.json"
        metadata = json.loads(manifest.read_text())
        metadata["emailAddress"] = "bob@example.com"
        manifest.write_text(json.dumps(metadata))
        queued = [
            json.loads(line)
            for f in sidecar.get_queue_dir().glob("*.jsonl")
            for line in f.read_text().splitlines()
        ]
        assert queued[0]["payload"]["events"][0]["account_id"] == "alice@example.com"


def test_collection_adds_cowork_without_cli_watermark(tmp_path, monkeypatch):
    from scripts import sidecar

    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    base = tmp_path / "Library/Application Support/Claude/local-agent-mode-sessions"
    session(base)
    monkeypatch.setattr(sidecar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    monkeypatch.setattr(sidecar, "_extract_events_for_provider", lambda **kw: 0)
    monkeypatch.setattr(sidecar, "_post_credential_manifest", lambda **kw: None)
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda *a: [])
    result = sidecar.run_collection({"providers": ["anthropic"]}, ["anthropic"])
    assert [e["event_id"] for e in result.events] == ["msg-a"]
    assert result.cowork_batch is not None


def test_idle_unresolved_origin_remains_visible_after_queue_ownership(tmp_path):
    base = tmp_path / "desktop"
    session(base, email=None)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    idle = collect(base, state)
    assert idle.events == []
    assert idle.blocked[0]["reason"] == "events_untagged"


def test_malformed_usage_does_not_consume_later_valid_events(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    bad = record("bad")
    bad["message"]["usage"]["input_tokens"] = "not-a-number"
    path.write_text(json.dumps(bad) + "\n" + json.dumps(record()) + "\n")
    batch = collect(base, tmp_path / "state.json")
    assert [e["event_id"] for e in batch.events] == ["msg-a"]


def test_one_large_complete_record_cannot_starve_backfill(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    item = record()
    item["message"]["content"] = [{"type": "text", "text": "x" * 2000}]
    path.write_text(json.dumps(item) + "\n")
    assert len(collect(base, tmp_path / "state.json", max_bytes=1000).events) == 1


def test_partial_batch_ack_cannot_skip_remaining_events(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    with path.open("a") as f:
        f.write(json.dumps(record("msg-b")) + "\n")
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events[:1])
    assert len(collect(base, state).events) == 2
    batch.mark_owned(batch.events[1:])
    assert collect(base, state).events == []


@pytest.mark.parametrize("clock", ["same-tick", "advanced"])
def test_same_inode_rewrite_after_shared_header_is_not_skipped(tmp_path, clock):
    base = tmp_path / "desktop"
    path = session(base)
    header = json.dumps({"type": "user", "padding": "x" * 300}) + "\n"
    path.write_text(header + json.dumps(record("old")) + "\n")
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    recorded = path.stat().st_mtime_ns
    path.write_text(header + json.dumps(record("new")) + "\n")
    # "same-tick" puts the rewrite inside the recorded filesystem timestamp
    # tick, where the mtime cannot tell it from an untouched file; "advanced"
    # moves the mtime on. Either way the rewrite must be noticed by content.
    target = recorded if clock == "same-tick" else recorded + 1_000_000_000
    try:
        os.utime(path, ns=(path.stat().st_atime_ns, target))
    except OSError:
        pytest.skip("mtime not settable on this filesystem")
    assert [e["event_id"] for e in collect(base, state).events] == ["new"]


@pytest.mark.parametrize("size", [8 * 1024, 32 * 1024])
def test_same_inode_rewrite_at_the_end_of_a_sub_window_transcript_is_not_skipped(tmp_path, size):
    base = tmp_path / "desktop"
    path = session(base)
    old = json.dumps(record("old")) + "\n"
    overhead = len(json.dumps({"type": "user", "padding": ""})) + 1
    body = json.dumps({"type": "user", "padding": "x" * (size - len(old) - overhead)}) + "\n"
    assert len(body) + len(old) == size  # the committed region is exactly `size` bytes
    path.write_text(body + old)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    recorded = path.stat().st_mtime_ns
    path.write_text(body + json.dumps(record("new")) + "\n")
    try:
        os.utime(path, ns=(path.stat().st_atime_ns, recorded))
    except OSError:
        pytest.skip("mtime not settable on this filesystem")
    assert [e["event_id"] for e in collect(base, state).events] == ["new"]


def test_same_inode_rewrite_at_the_end_of_a_large_transcript_is_not_skipped(tmp_path):
    base = tmp_path / "desktop"
    path = session(base)
    body = json.dumps({"type": "user", "padding": "x" * 4000}) + "\n"
    body = body * 100  # past the fingerprint window, so the tail carries the rewrite
    path.write_text(body + json.dumps(record("old")) + "\n")
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    recorded = path.stat().st_mtime_ns
    path.write_text(body + json.dumps(record("new")) + "\n")
    try:
        os.utime(path, ns=(path.stat().st_atime_ns, recorded))
    except OSError:
        pytest.skip("mtime not settable on this filesystem")
    assert [e["event_id"] for e in collect(base, state).events] == ["new"]


def test_fingerprint_covers_the_head_and_the_tail_of_the_committed_region(tmp_path):
    from scripts.sidecar_pkg.cowork_events import _fingerprint

    path = tmp_path / "transcript.jsonl"
    body = b"a" * (256 * 1024)
    path.write_bytes(body + b"tail-one")
    window = _fingerprint(path, path.stat().st_size)
    path.write_bytes(body + b"tail-two")
    assert _fingerprint(path, path.stat().st_size) != window  # the tail carries a rewrite
    path.write_bytes(b"b" + body[1:] + b"tail-one")
    assert _fingerprint(path, path.stat().st_size) != window  # the head carries one too
    middle = bytearray(body)
    middle[100000:100010] = b"0123456789"
    path.write_bytes(bytes(middle) + b"tail-one")
    # The window is bounded on purpose, so a rewrite that touches neither the
    # head nor the tail of the committed region is not detected. Tracked in
    # the follow-up issue linked from the pull request.
    assert _fingerprint(path, path.stat().st_size) == window
    # A region that fits the window is covered in full.
    small = b"s" * (32 * 1024)
    path.write_bytes(small)
    digest = _fingerprint(path, path.stat().st_size)
    changed = bytearray(small)
    changed[16 * 1024 : 16 * 1024 + 4] = b"0123"
    path.write_bytes(bytes(changed))
    assert _fingerprint(path, path.stat().st_size) != digest


def test_queue_sync_failure_does_not_own_checkpoint(tmp_path, monkeypatch):
    from scripts import sidecar

    base = tmp_path / "desktop"
    session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    result = sidecar.CollectionResult([], batch.events, 0, [], batch)
    monkeypatch.setattr(sidecar, "run_collection", lambda *a, **kw: result)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway"))
    monkeypatch.setattr(sidecar, "_tail_log", lambda n: [])
    monkeypatch.setattr(
        sidecar, "http_post_signed_with_retry", lambda *a, **kw: (False, {"detail": "offline"}, 0)
    )

    def sync_failure(fd):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(sidecar.os, "fsync", sync_failure)
    runner = sidecar.DaemonRunner({"api_url": "http://localhost:9999", "api_key": "synthetic"})
    assert runner.run_once(providers=["anthropic"]) is False
    assert len(collect(base, state).events) == 1


def test_cowork_batch_replay_uses_server_event_deduplication(tmp_path):
    from app.models.db import UsageEvent
    from app.models.schemas import UsageEventPush
    from app.services.event_ingestor import EventIngestor

    base = tmp_path / "desktop"
    session(base)
    batch = collect(base, tmp_path / "state.json")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            pushes = [UsageEventPush(**e) for e in batch.events]
            assert EventIngestor(db).ingest(pushes, sidecar_id="mac-test").events_inserted == 1
            assert EventIngestor(db).ingest(pushes, sidecar_id="mac-test").events_duplicate == 1
            rows = db.exec(select(UsageEvent)).all()
            assert len(rows) == 1
            assert rows[0].tokens_input == 11
            assert rows[0].tokens_output == 7
            assert rows[0].tokens_cache_read == 23
            assert rows[0].tokens_cache_create == 13
    finally:
        engine.dispose()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX directory fsync")
def test_queue_ownership_syncs_file_and_directory(tmp_path, monkeypatch):
    import os
    import stat

    from scripts import sidecar

    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway"))
    real_sync = os.fsync
    synced = []

    def sync(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_sync(fd)

    monkeypatch.setattr(sidecar.os, "fsync", sync)
    assert sidecar.queue_push({"events": [{"provider_id": "anthropic", "event_id": "x"}]})
    assert False in synced
    assert True in synced
