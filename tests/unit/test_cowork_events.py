"""Exact Cowork usage from synthetic, content-free Desktop sessions."""

import json
import os
from datetime import UTC, date, datetime

import pytest
import time_machine
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


def test_cli_overlap_requires_the_larger_payload_before_checkpointing(tmp_path):
    base = tmp_path / "desktop"
    session(base)
    state = tmp_path / "state.json"
    full = collect(base, state).events[0]
    partial = {**full, "tokens_output": 1}

    batch = collect(base, state, existing_events=[partial])
    assert batch.events == [full]
    batch.mark_owned([partial])
    assert collect(base, state).events == [full]
    batch.mark_owned([full])
    assert collect(base, state).events == []

    state.unlink()
    batch = collect(base, state, existing_events=[full])
    assert batch.events == []
    batch.mark_owned([])
    assert collect(base, state).events == [full]
    batch.mark_owned([full])
    assert collect(base, state).events == []


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


@pytest.mark.parametrize("size", [8 * 1024, 32 * 1024, 68 * 1024, 68 * 1024 + 1])
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
    # head nor the tail of the committed region is not detected by it; the
    # Cowork reader's chunk audit covers that (see the audit tests below).
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


@pytest.mark.parametrize("source", ["cowork", "cli", "cli-custom", "cli-same-stamp"])
@time_machine.travel("2026-07-02T12:00:00Z")
def test_streamed_usage_reaches_ingestion_across_sidecar_cycles(tmp_path, monkeypatch, source):
    from app.core.db import configure_sqlite_engine
    from app.models.db import ProviderPricing, UsageEvent, UsagePeriodRollup
    from app.models.schemas import UsageEventPush
    from app.services.event_ingestor import EventIngestor
    from scripts import sidecar
    from scripts.sidecar_pkg.credentials import CredentialCache
    from scripts.sidecar_pkg.event_extractors import anthropic

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway"))
    monkeypatch.setattr(
        sidecar.platform, "system", lambda: "Darwin" if source == "cowork" else "Linux"
    )
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    monkeypatch.setattr(sidecar, "_post_credential_manifest", lambda **kw: None)
    monkeypatch.setattr(sidecar, "_tail_log", lambda n: [])
    monkeypatch.setattr(sidecar, "queue_flush", lambda *a, **kw: None)
    monkeypatch.setattr(sidecar.DaemonRunner, "_apply_ingest_instructions", lambda *a, **kw: None)
    monkeypatch.setitem(
        sidecar._LEGACY_EVENT_ACCOUNT_DISCOVERY, "anthropic", lambda: "alice@example.com"
    )
    cache = CredentialCache()
    cache.replace(accounts={"anthropic": ["alice@example.com"]})
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)

    root = tmp_path / "work-claude" if source == "cli-custom" else tmp_path / ".claude"
    monkeypatch.setattr(
        sidecar, "custom_login_dirs", lambda *_: [root] if source == "cli-custom" else []
    )
    if source == "cowork":
        path = session(tmp_path / "Library/Application Support/Claude/local-agent-mode-sessions")
    else:
        path = root / "projects" / "cli-one.jsonl"
        path.parent.mkdir(parents=True)
        (root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "alice@example.com"}})
        )

    first_stamp = "2026-07-01T23:59:59Z"
    # The CLI paths repeat exactly the timestamp they already pushed.
    # Cowork also exercises a later record that falls on a different day.
    final_stamp = "2026-07-02T00:00:01Z" if source == "cowork" else first_stamp
    first = record(ts=first_stamp)
    first_output = 17 if source == "cli-same-stamp" else 7
    first["message"]["usage"]["output_tokens"] = first_output
    path.write_bytes((json.dumps(first) + "\n").encode())

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            rates = {
                "provider_id": "anthropic",
                "model_id": "sonnet-4.5",
                "input_per_mtok": 3,
                "cache_read_per_mtok": 0.3,
                "cache_create_per_mtok": 3.75,
                "cache_create_1h_per_mtok": 6,
            }
            db.add(ProviderPricing(**rates, effective_from=date(2026, 7, 1), output_per_mtok=15))
            db.add(ProviderPricing(**rates, effective_from=date(2026, 7, 2), output_per_mtok=30))
            db.commit()
            ingestor = EventIngestor(db)
            sent = []
            results = []
            reject = False

            def post(_url, payload, *_args, **_kwargs):
                pushes = [UsageEventPush(**e) for e in payload["events"]]
                sent.append(pushes)
                if reject:
                    return True, {"events_error": True}, 200
                results.append(ingestor.ingest(pushes, sidecar_id="same-host"))
                return True, {}, 200

            config = {
                "api_url": "http://localhost:9999",
                "api_key": "synthetic",  # pragma: allowlist secret
            }
            monkeypatch.setattr(sidecar, "http_post_signed_with_retry", post)
            runner = sidecar.DaemonRunner(config)
            assert runner.run_once(providers=["anthropic"])
            assert results[-1].events_inserted == 1
            (partial,) = sent[-1]
            row = db.exec(select(UsageEvent)).one()
            row_id = row.id
            assert row.tokens_output == first_output

            final = record(ts=final_stamp)
            final["message"]["usage"].update({"output_tokens": 41, "input_tokens": 19})
            final_line = (json.dumps(final) + "\n").encode()
            if source == "cli-same-stamp":
                # Simulate a same-size, same-inode rewrite within one timestamp tick.
                stamp = anthropic._file_stamp(path.stat())
                assert stamp[2] == len(final_line)
                monkeypatch.setattr(anthropic, "_file_stamp", lambda _stat: stamp)
            with path.open("wb" if source in {"cli-custom", "cli-same-stamp"} else "ab") as file:
                file.write(final_line)

            reject = True
            runner.run_once(providers=["anthropic"])
            assert [push.tokens_output for push in sent[-1]] == [41]
            reject = False
            # A new runner still reads the last accepted cursor after a failure.
            runner = sidecar.DaemonRunner(config)
            assert runner.run_once(providers=["anthropic"])
            assert results[-1].events_refreshed == 1
            (updated,) = sent[-1]
            assert updated.event_id == partial.event_id
            assert runner.run_once(providers=["anthropic"])
            assert sent[-1] == []

            assert ingestor.ingest([updated, partial], sidecar_id="same-host").events_duplicate == 2
            foreign = updated.model_copy(
                update={"account_id": "foreign@example.com", "tokens_output": 90}
            )
            assert ingestor.ingest([foreign], sidecar_id="other-host").events_duplicate == 1
            assert (
                ingestor.ingest(
                    [foreign.model_copy(update={"account_id": "alice@example.com"})],
                    sidecar_id="other-host",
                ).events_duplicate
                == 1
            )

            expected_cost = round((19 * 3 + 41 * 15 + 23 * 0.3 + 5 * 6 + 8 * 3.75) / 1_000_000, 6)

            def assert_final(account):
                event = db.exec(select(UsageEvent)).one()
                assert event.id == row_id
                assert event.sidecar_id == "same-host"
                assert event.account_id == account
                assert event.ts == datetime(2026, 7, 1, 23, 59, 59, tzinfo=UTC)
                assert (event.tokens_input, event.tokens_output) == (19, 41)
                assert event.cost_usd == pytest.approx(expected_cost)
                assert event.cost_output == pytest.approx(41 * 15 / 1_000_000)
                rollups = db.exec(select(UsagePeriodRollup)).all()
                active = [r for r in rollups if r.account_id == account]
                assert len(active) == 20
                assert {r.period_key for r in active if r.period_type == "day"} == {"2026-07-01"}
                for r in active:
                    assert (r.msgs, r.tokens_input, r.tokens_output) == (1, 19, 41)
                    assert r.cost_usd == pytest.approx(expected_cost)
                for r in rollups:
                    if r.account_id != account:
                        assert (r.msgs, r.tokens_input, r.tokens_output) == (0, 0, 0)
                        assert r.cost_usd == pytest.approx(0)

            assert_final("alice@example.com")
            retag = partial.model_copy(update={"account_id": "retagged@example.com"})
            assert ingestor.ingest([retag], sidecar_id="same-host").events_reattributed == 1
            assert_final("retagged@example.com")
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


# Middle-of-history rewrites (#802): the chunk audit re-reads committed chunks.

_PAD = json.dumps({"type": "user", "padding": "x" * 4000}) + "\n"


def _large_session(base, n=120):
    """A transcript of ~1 MiB with one assistant record far from the head and the tail."""
    path = session(base)
    path.write_text(
        _PAD * n + json.dumps(record("mid-a")) + "\n" + _PAD * n + json.dumps(record("end")) + "\n"
    )
    return path


def _rewrite_middle(path, *, append=""):
    stat = path.stat()
    data = path.read_bytes().replace(b'"mid-a"', b'"mid-b"')
    path.write_bytes(data + append.encode())
    if not append:
        try:
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError:
            pytest.skip("mtime not settable on this filesystem")
        assert path.stat().st_size == stat.st_size


def _ids(batch):
    return {e["event_id"] for e in batch.events}


def test_middle_rewrite_with_same_size_and_mtime_is_replayed(tmp_path):
    base = tmp_path / "desktop"
    path = _large_session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    assert _ids(batch) == {"mid-a", "end"}
    batch.mark_owned(batch.events)
    assert collect(base, state).events == []
    _rewrite_middle(path)
    assert "mid-b" in _ids(collect(base, state))


def test_middle_rewrite_plus_append_is_replayed(tmp_path):
    base = tmp_path / "desktop"
    path = _large_session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    _rewrite_middle(path, append=json.dumps(record("tail")) + "\n")
    assert {"mid-b", "tail"} <= _ids(collect(base, state))


def test_state_without_chunks_is_backfilled_not_replayed(tmp_path):
    base = tmp_path / "desktop"
    _large_session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    data = json.loads(state.read_text())
    for cursor in data["files"].values():
        del cursor["chunks"]  # a cursor written before the audit existed
    state.write_text(json.dumps(data))
    assert collect(base, state).events == []
    (cursor,) = json.loads(state.read_text())["files"].values()
    assert cursor["chunks"] and all(isinstance(c, str) for c in cursor["chunks"])


def test_idle_transcript_audit_advances_across_cycles_and_finds_a_rewrite(tmp_path):
    from scripts.sidecar_pkg.cowork_events import _CHUNK_BYTES

    base = tmp_path / "desktop"
    path = _large_session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    data = json.loads(state.read_text())
    (cursor,) = data["files"].values()
    count = len(cursor["chunks"])
    assert count >= 4
    cursor["chunks"] = [None] * count
    state.write_text(json.dumps(data))
    for cycle in range(1, count + 1):
        # One chunk per scan: the idle file has no events, yet its audit
        # progress is persisted and moves on.
        assert collect(base, state, audit_bytes=_CHUNK_BYTES).events == []
        (cursor,) = json.loads(state.read_text())["files"].values()
        assert sum(c is not None for c in cursor["chunks"]) == cycle
    _rewrite_middle(path)
    found = set()
    for _ in range(count):
        found |= _ids(collect(base, state, audit_bytes=_CHUNK_BYTES))
        if found:
            break
    assert "mid-b" in found


def _org_session(base, org, pads, mid="m"):
    root = base / "account-one" / org
    root.mkdir(parents=True)
    (root / "local_one.json").write_text(
        json.dumps({"sessionId": "local_one", "cliSessionId": "cli-one", "emailAddress": "a@x.io"})
    )
    path = root / "local_one" / ".claude" / "projects" / "p" / "cli-one.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_PAD * pads + json.dumps(record(mid)) + "\n" + _PAD * pads + "\n")
    return path


def _cursors(state):
    return json.loads(state.read_text())["files"]


def test_audit_budget_is_shared_fairly_across_transcripts(tmp_path, monkeypatch):
    from scripts.sidecar_pkg import cowork_events

    base = tmp_path / "desktop"
    paths = [str(_org_session(base, org, 100, f"m-{org}").resolve()) for org in ("o1", "o2", "o3")]
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    reads = []
    real = cowork_events._chunk_digest
    monkeypatch.setattr(
        cowork_events, "_chunk_digest", lambda f, i, end: reads.append(i) or real(f, i, end)
    )
    for _ in range(3):
        before = {k: _cursors(state)[k].get("audit_next", 0) for k in paths}
        reads.clear()
        assert collect(base, state, audit_bytes=3 * cowork_events._CHUNK_BYTES).events == []
        assert len(reads) == 3
        after = _cursors(state)
        # Every transcript advanced by one chunk in the same scan.
        assert all(after[k]["audit_next"] != before[k] for k in paths)


def test_transcript_larger_than_the_budget_does_not_starve_the_others(tmp_path):
    from scripts.sidecar_pkg.cowork_events import _CHUNK_BYTES

    base = tmp_path / "desktop"
    big = str(_org_session(base, "big", 400, "m-big").resolve())  # ~13 chunks
    small = _org_session(base, "small", 60, "mid-a")  # ~4 chunks
    state = tmp_path / "state.json"
    batch = collect(base, state, max_bytes=10**9)
    batch.mark_owned(batch.events)
    budget = 4 * _CHUNK_BYTES
    count = len(_cursors(state)[big]["chunks"])
    assert count * _CHUNK_BYTES > 2 * budget
    for _ in range(count):
        assert collect(base, state, audit_bytes=budget).events == []
    # The big transcript's pass spans several scans and still completes.
    assert "audited" in _cursors(state)[big]
    _rewrite_middle(small)
    found = set()
    for _ in range(10):
        found |= _ids(collect(base, state, audit_bytes=budget))
    assert "mid-b" in found


def test_audit_walk_survives_a_commit_on_a_growing_transcript(tmp_path):
    from scripts.sidecar_pkg.cowork_events import _CHUNK_BYTES

    base = tmp_path / "desktop"
    path = _large_session(base)
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    assert collect(base, state, audit_bytes=_CHUNK_BYTES).events == []
    key = str(path.resolve())
    walked = _cursors(state)[key]["audit_next"]
    with path.open("a") as f:
        f.write(json.dumps(record("tail")) + "\n")
    batch = collect(base, state, audit_bytes=_CHUNK_BYTES)
    assert _ids(batch) == {"tail"}
    batch.mark_owned(batch.events)
    assert _cursors(state)[key]["audit_next"] == walked + 1


def test_many_transcripts_on_a_small_budget_all_complete_a_pass(tmp_path):
    from scripts.sidecar_pkg.cowork_events import _CHUNK_BYTES

    base = tmp_path / "desktop"
    paths = [str(_org_session(base, f"o{i}", 40, f"m{i}").resolve()) for i in range(6)]
    state = tmp_path / "state.json"
    batch = collect(base, state)
    batch.mark_owned(batch.events)
    # Fewer chunks per scan than transcripts: each scan serves a different
    # subset, and every transcript still completes its pass.
    for _ in range(25):
        assert collect(base, state, audit_bytes=2 * _CHUNK_BYTES).events == []
    cursors = _cursors(state)
    assert all("audited" in cursors[k] for k in paths)
