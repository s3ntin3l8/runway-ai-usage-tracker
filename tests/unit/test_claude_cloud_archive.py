"""Explicit terminal preservation is atomic, encrypted and cannot replay provider work."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore
from tests.unit import test_claude_cloud_verification as source_fixtures
from tests.unit.test_claude_cloud_checkpoints import setup_store

source_environment = source_fixtures.source_environment


def terminal(tmp_path):
    store, state, path, crypto, clock = setup_store(tmp_path)
    state.phase = "stopped"
    state.outcome = "unsupported_contract"
    state.in_flight = True
    state.requests = 90
    handle = store.create(state)
    return store, state, path, crypto, clock, handle


def test_archive_preserves_exact_ciphertext_and_survives_restart(tmp_path):
    store, state, path, crypto, clock, handle = terminal(tmp_path)
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT state_encrypted FROM claude_cloud_verification").fetchone()[0]
    assert store.archive_preview(handle) == {
        "state": "terminal",
        "job_handle": handle,
        "revision": 0,
    }
    assert store.archive(handle, 0) == 0
    assert store.archive(handle, 0) == 0
    assert store.current_handle() is None
    restarted = CheckpointStore(path, crypto, clock=lambda: clock[0])
    assert restarted.archived_snapshot(handle) == state
    assert restarted.archive_preview(handle) == {
        "state": "archived",
        "job_handle": handle,
        "revision": 0,
    }
    with sqlite3.connect(path) as db:
        archived = db.execute(
            "SELECT state_encrypted FROM claude_cloud_verification_archives"
        ).fetchone()[0]
    assert archived == original and archived.startswith("gAAAAA")
    assert b"private-holder" not in path.read_bytes()


def test_idempotent_archive_cannot_touch_a_new_current_job(tmp_path):
    store, state, _, _, _, handle = terminal(tmp_path)
    store.archive(handle, 0)
    state.phase = "listing"
    state.outcome = "pending"
    state.in_flight = False
    new = store.create(state)
    store.archive(handle, 0)
    assert new != handle and store.current_handle() == new
    with pytest.raises(CheckpointError, match="^checkpoint_changed$"):
        store.archive(handle, 1)
    assert store.current_handle() == new


def test_nonterminal_or_leased_checkpoint_cannot_be_archived(tmp_path):
    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    assert store.archive_preview(handle)["state"] == "not_terminal"
    with pytest.raises(CheckpointError, match="^not_terminal$"):
        store.archive(handle, 0)
    claim = store.claim(handle)
    claim.state.phase = "stopped"
    claim.state.outcome = "unsupported_contract"
    store.save(claim)
    with pytest.raises(CheckpointError, match="^busy$"):
        store.archive(handle, claim.revision)
    store.release(claim)
    store.archive(handle, claim.revision)


@pytest.mark.parametrize("operation", ["INSERT", "DELETE"])
def test_failed_archive_transaction_leaves_original_intact(tmp_path, operation):
    store, state, path, _, _, handle = terminal(tmp_path)
    table = (
        "claude_cloud_verification_archives"
        if operation == "INSERT"
        else "claude_cloud_verification"
    )
    with sqlite3.connect(path) as db:
        db.execute(
            f"CREATE TRIGGER fail_archive BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.archive(handle, 0)
    assert store.current_handle() == handle and store.snapshot(handle) == state
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM claude_cloud_verification_archives").fetchone()[0] == 0
        )


def test_concurrent_archive_is_idempotent_and_rejects_wrong_revision(tmp_path):
    store, state, path, _, _, handle = terminal(tmp_path)
    with pytest.raises(CheckpointError, match="^checkpoint_changed$"):
        store.archive(handle, 1)
    assert store.snapshot(handle) == state
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: store.archive(handle, 0), range(2)))
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM claude_cloud_verification_archives").fetchone()[0] == 1
        )


def test_failed_readback_rolls_back_archive_and_keeps_original(tmp_path):
    store, state, path, _, _, handle = terminal(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER corrupt_archive AFTER INSERT ON claude_cloud_verification_archives "
            "BEGIN UPDATE claude_cloud_verification_archives SET state_encrypted='corrupt'; END"
        )
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.archive(handle, 0)
    assert store.snapshot(handle) == state
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM claude_cloud_verification_archives").fetchone()[0] == 0
        )


def test_failed_commit_rolls_back_both_copy_and_slot_release(tmp_path, monkeypatch):
    from contextlib import contextmanager

    store, state, path, _, _, handle = terminal(tmp_path)
    original = store._connection

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("synthetic commit failure")

    @contextmanager
    def connection():
        with original() as db:
            yield Connection(db)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connection", connection)
        with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
            store.archive(handle, 0)
    assert store.current_handle() == handle and store.snapshot(handle) == state
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT count(*) FROM claude_cloud_verification_archives").fetchone()[0] == 0
        )


def test_archive_capacity_fails_closed_without_deleting_history(tmp_path, monkeypatch):
    from app.services import claude_cloud_checkpoints as checkpoints

    store, state, _, _, _, handle = terminal(tmp_path)
    monkeypatch.setattr(checkpoints, "MAX_ARCHIVES", 0)
    with pytest.raises(CheckpointError, match="^archive_limit$"):
        store.archive(handle, 0)
    assert store.snapshot(handle) == state


def test_archive_corruption_and_disabled_crypto_never_release_slot(tmp_path, monkeypatch):
    store, state, path, _, _, handle = terminal(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE claude_cloud_verification SET state_encrypted='not-encrypted'")
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.archive(handle, 0)
    assert store.current_handle() == handle
    monkeypatch.setattr(
        store,
        "_require_encryption",
        lambda: (_ for _ in ()).throw(CheckpointError("encryption_required")),
    )
    with pytest.raises(CheckpointError, match="^encryption_required$"):
        store.archive(handle, 0)
    assert store.current_handle() == handle


@pytest.mark.asyncio
async def test_fresh_service_job_gets_new_defaults_after_explicit_archival(source_environment):
    from tests.unit.test_claude_cloud_verification import start, store_for

    store = store_for(source_environment)
    clock = [1000.0]
    store.clock = lambda: clock[0]
    old = await start(store)
    before = store.snapshot(old)
    claim = store.claim(old)
    claim.state.phase = "stopped"
    claim.state.outcome = "unsupported_contract"
    claim.state.requests = 90
    claim.state.in_flight = True
    store.save(claim)
    store.release(claim)
    store.archive(old, claim.revision)
    clock[0] += 15
    new = await start(store)
    after = store.snapshot(new)
    assert new != old and after.job_key != before.job_key
    assert after.source_digest != before.source_digest
    assert after.created_at == 1015 and after.deadline == 2815
    assert after.requests == after.total_bytes == 0 and after.next_cursor is None
    assert store.archived_snapshot(old).requests == 90


def test_existing_database_initialization_adds_archive_table_without_rewriting_current(tmp_path):
    from sqlmodel import SQLModel, create_engine

    store, state, path, _, _, handle = terminal(tmp_path)
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT state_encrypted FROM claude_cloud_verification").fetchone()[0]
        db.execute("DROP TABLE claude_cloud_verification_archives")
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT state_encrypted FROM claude_cloud_verification").fetchone()[0]
            == original
        )
    store.archive(handle, 0)
    assert store.archived_snapshot(handle) == state


def test_real_disabled_encryption_cannot_preview_or_archive(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.core.encryption import EncryptionService

    store, _, path, _, _, handle = terminal(tmp_path)
    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", "")
    disabled = CheckpointStore(path, EncryptionService())
    with pytest.raises(CheckpointError, match="^encryption_required$"):
        disabled.archive_preview(handle)
    with pytest.raises(CheckpointError, match="^encryption_required$"):
        disabled.archive(handle, 0)
    assert store.current_handle() == handle


def test_archived_handle_collision_cannot_create_a_conflicting_job(tmp_path, monkeypatch):
    from app.services import claude_cloud_checkpoints as checkpoints

    store, state, _, _, _, handle = terminal(tmp_path)
    store.archive(handle, 0)
    monkeypatch.setattr(checkpoints.secrets, "token_hex", lambda count: handle)
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.create(state)
    assert store.current_handle() is None and store.archived_snapshot(handle) == state


@pytest.mark.parametrize("operation", ["cancel", "start", "step"])
def test_archive_races_never_acknowledge_a_missing_copy_or_touch_a_new_job(tmp_path, operation):
    from threading import Barrier

    store, state, path, _, _, handle = terminal(tmp_path)
    barrier = Barrier(2)

    def archive():
        barrier.wait(timeout=2)
        try:
            store.archive(handle, 0)
            return "archived"
        except CheckpointError as exc:
            return str(exc)

    def other():
        barrier.wait(timeout=2)
        try:
            if operation == "cancel":
                store.cancel(handle)
                return "cancelled"
            if operation == "step":
                store.claim(handle)
                pytest.fail("terminal job was claimed")
            return store.create(state)
        except CheckpointError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(archive)
        competing = pool.submit(other)
        result, outcome = saved.result(timeout=5), competing.result(timeout=5)
    with sqlite3.connect(path) as db:
        copies = db.execute("SELECT count(*) FROM claude_cloud_verification_archives").fetchone()[0]
    if result == "archived":
        assert copies == 1 and store.archived_snapshot(handle) == state
    else:
        assert operation == "cancel" and outcome == "cancelled" and result == "not_found"
        assert copies == 0
    if operation == "start" and len(outcome) == 32:
        assert store.current_handle() == outcome and outcome != handle
    else:
        assert store.current_handle() is None
    if operation == "step":
        assert outcome in ("terminal", "not_found")


def test_wrong_table_nonterminal_ciphertext_cannot_be_acknowledged_as_archived(tmp_path):
    store, state, path, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO claude_cloud_verification_archives "
            "(job_handle,revision,state_encrypted,archived_at) "
            "SELECT job_handle,revision,state_encrypted,1000 FROM claude_cloud_verification"
        )
    for operation in (
        lambda: store.archive_preview(handle),
        lambda: store.archive(handle, 0),
        lambda: store.archived_snapshot(handle),
    ):
        with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
            operation()
    assert store.current_handle() == handle and store.snapshot(handle) == state


@pytest.mark.parametrize("revision", [True, -1, "0"])
def test_invalid_local_revision_is_distinct_from_a_stale_checkpoint(tmp_path, revision):
    store, state, _, _, _, handle = terminal(tmp_path)
    with pytest.raises(CheckpointError, match="^invalid_revision$"):
        store.archive(handle, revision)
    assert store.snapshot(handle) == state


def test_archive_byte_limit_leaves_original_and_prior_history_intact(tmp_path, monkeypatch):
    from app.services import claude_cloud_checkpoints as checkpoints

    store, state, path, _, _, handle = terminal(tmp_path)
    store.archive(handle, 0)
    current = store.create(state)
    with sqlite3.connect(path) as db:
        used = db.execute(
            "SELECT SUM(LENGTH(CAST(state_encrypted AS BLOB))) FROM claude_cloud_verification_archives"
        ).fetchone()[0]
    monkeypatch.setattr(checkpoints, "MAX_ARCHIVED_BYTES", used)
    with pytest.raises(CheckpointError, match="^archive_limit$"):
        store.archive(current, 0)
    assert store.current_handle() == current and store.snapshot(current) == state
    assert store.archived_snapshot(handle) == state
