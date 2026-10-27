"""Encrypted durable intent, exclusion and recovery for explicit verification jobs."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from sqlmodel import create_engine

from app.core.encryption import EncryptionService


def setup_store(tmp_path, *, now=None):
    from app.models.claude_cloud_verification import VerificationState
    from app.models.db import ClaudeCloudVerification
    from app.services.claude_cloud_checkpoints import CheckpointStore

    path = tmp_path / "checkpoint.db"
    engine = create_engine(f"sqlite:///{path}")
    ClaudeCloudVerification.metadata.create_all(engine)
    engine.dispose()
    key = Fernet.generate_key().decode()
    crypto = EncryptionService(key)
    clock = now if now is not None else [1000.0]
    store = CheckpointStore(path, crypto, clock=lambda: clock[0])
    state = VerificationState(
        account_id="private-holder@example.com",
        source_id="private-source",
        job_key="a" * 64,
        source_digest="b" * 64,
        created_at=1000.0,
        deadline=2800.0,
    )
    return store, state, path, crypto, clock


def test_checkpoint_is_encrypted_and_survives_store_restart(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointStore

    store, state, path, crypto, clock = setup_store(tmp_path)
    handle = store.create(state)
    assert len(handle) == 32
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT state_encrypted FROM claude_cloud_verification").fetchone()[0]
    assert raw.startswith("gAAAAA")
    assert "private-" not in raw and state.job_key not in raw
    restarted = CheckpointStore(path, crypto, clock=lambda: clock[0])
    assert restarted.snapshot(handle) == state
    assert b"private-holder" not in path.read_bytes()


def test_only_one_thread_or_store_claims_the_same_job(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)

    def claim():
        try:
            return store.claim(handle)
        except CheckpointError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(2)))
    assert sum(value == "busy" for value in outcomes if isinstance(value, str)) == 1
    owners = [value for value in outcomes if not isinstance(value, str)]
    assert len(owners) == 1
    store.release(owners[0])


def test_saved_page_can_resume_but_committed_intent_cannot_replay(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore

    store, state, path, crypto, clock = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.requests = 1
    owner.state.in_flight = True
    store.save(owner)
    store.release(owner)
    restarted = CheckpointStore(path, crypto, clock=lambda: clock[0])
    with pytest.raises(CheckpointError, match="^interrupted$"):
        restarted.claim(handle)
    stopped = restarted.snapshot(handle)
    assert stopped.phase == "stopped" and stopped.outcome == "interrupted"
    assert stopped.requests == 1
    assert stopped.in_flight is True


def test_clean_checkpoint_keeps_the_cursor_and_budget_across_restart(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointStore

    store, state, path, crypto, clock = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.next_cursor = "private-next-cursor"
    owner.state.requests = 2
    owner.state.total_bytes = 123
    store.save(owner)
    store.release(owner)
    restarted = CheckpointStore(path, crypto, clock=lambda: clock[0])
    second = restarted.claim(handle)
    assert second.state.next_cursor == "private-next-cursor"
    assert second.state.requests == 2 and second.state.total_bytes == 123
    restarted.release(second)


@pytest.mark.parametrize("replacement", ['{"account_id":"private-plaintext"}', "gAAAAA-invalid"])
def test_plaintext_or_tampered_checkpoint_never_uses_legacy_fallback(tmp_path, replacement):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, path, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE claude_cloud_verification SET state_encrypted=?", (replacement,))
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.snapshot(handle)


def test_disabled_encryption_prevents_any_checkpoint_write(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore

    _, state, path, _, _ = setup_store(tmp_path)
    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", "")
    disabled = EncryptionService()
    store = CheckpointStore(path, disabled)
    with pytest.raises(CheckpointError, match="^encryption_required$"):
        store.create(state)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM claude_cloud_verification").fetchone()[0] == 0


def test_expired_lease_stops_the_old_job_and_rejects_late_writes(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, clock = setup_store(tmp_path)
    handle = store.create(state)
    old = store.claim(handle)
    clock[0] += 46
    with pytest.raises(CheckpointError, match="^interrupted$"):
        store.claim(handle)
    old.state.requests = 50
    with pytest.raises(CheckpointError, match="^lost_lease$"):
        store.save(old)
    assert store.snapshot(handle).requests == 0
    assert store.snapshot(handle).outcome == "interrupted"


def test_failed_encryption_does_not_commit_progress(tmp_path, monkeypatch):
    from app.core.encryption import EncryptionError
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, crypto, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)

    def failed(_):
        raise EncryptionError("private-failure")

    monkeypatch.setattr(crypto, "encrypt_string", failed)
    owner.state.requests = 3
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.save(owner)
    assert store.snapshot(handle).requests == 0
    store.release(owner)


def test_cancel_requires_correct_handle_and_no_active_step(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    with pytest.raises(CheckpointError, match="^busy$"):
        store.cancel(handle)
    with pytest.raises(CheckpointError, match="^not_found$"):
        store.cancel("f" * 32)
    store.release(owner)
    store.cancel(handle)
    with pytest.raises(CheckpointError, match="^not_found$"):
        store.snapshot(handle)


def test_checkpoint_rejects_transcripts_and_credentials_even_when_nested(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.history_events = 1
    owner.state.sequence.invalid = 1
    owner.state.usage_records.append(
        {
            "type": "assistant",
            "message": {"id": "a" * 64, "usage": False, "content": "private-transcript"},
        }
    )
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.save(owner)
    assert store.snapshot(handle).usage_records == []
    store.release(owner)


def test_job_deadline_does_not_restart_its_budget(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, clock = setup_store(tmp_path)
    handle = store.create(state)
    clock[0] = 2801.0
    with pytest.raises(CheckpointError, match="^expired$"):
        store.claim(handle)
    assert store.snapshot(handle).outcome == "expired"
    assert store.snapshot(handle).requests == 0


def test_a_recorded_equivalent_session_cannot_be_rewritten_or_cleared(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.equivalent_session = "cse_abc"
    store.save(owner)
    owner.state.equivalent_session = "cse_other"
    with pytest.raises(CheckpointError, match="^invalid_transition$"):
        store.save(owner)
    owner.state.equivalent_session = None
    with pytest.raises(CheckpointError, match="^invalid_transition$"):
        store.save(owner)
    store.release(owner)


def test_progress_and_identity_cannot_be_rolled_back_with_a_valid_lease(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.requests = 1
    store.save(owner)
    owner.state.requests = 0
    with pytest.raises(CheckpointError, match="^invalid_transition$"):
        store.save(owner)
    owner.state.requests = 1
    owner.state.source_id = "private-other-source"
    with pytest.raises(CheckpointError, match="^invalid_transition$"):
        store.save(owner)
    assert store.snapshot(handle).requests == 1
    store.release(owner)


def test_old_authenticated_ciphertext_cannot_replay_an_earlier_revision(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, path, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    with sqlite3.connect(path) as db:
        old = db.execute("SELECT state_encrypted FROM claude_cloud_verification").fetchone()[0]
    owner = store.claim(handle)
    owner.state.requests = 1
    store.save(owner)
    store.release(owner)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE claude_cloud_verification SET state_encrypted=?", (old,))
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        store.snapshot(handle)


def test_invalid_nested_data_never_appears_in_serializer_diagnostics(tmp_path):
    import warnings

    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    owner.state.history_events = 1
    owner.state.sequence.invalid = 1
    owner.state.usage_records.append(
        {
            "type": "assistant",
            "message": {"id": "a" * 64, "usage": False, "content": "private-serialization-secret"},
        }
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(CheckpointError):
            store.save(owner)
    assert caught == []
    store.release(owner)


@pytest.mark.parametrize("mutation", ["version", "usage_false", "parent_true"])
def test_private_schema_does_not_coerce_numeric_booleans(tmp_path, mutation):
    from pydantic import ValidationError

    from app.models.claude_cloud_verification import VerificationState

    _, state, _, _, _ = setup_store(tmp_path)
    value = state.model_dump()
    if mutation == "version":
        value["version"] = True
    else:
        value["history_events"] = 1
        value["sequence"]["invalid"] = 1
        value["usage_records"] = [
            {"type": "assistant", "message": {"id": "a" * 64, "usage": False}}
        ]
        if mutation == "usage_false":
            value["usage_records"][0]["message"]["usage"] = 0
        else:
            value["usage_records"][0]["parent_tool_use_id"] = 1
    with pytest.raises(ValidationError):
        VerificationState.model_validate(value)


def test_another_process_cannot_claim_a_live_lease(tmp_path):
    import json
    import subprocess
    import sys

    store, state, path, crypto, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    program = """
import json,sys
from pathlib import Path
from app.core.encryption import EncryptionService
from app.services.claude_cloud_checkpoints import CheckpointStore,CheckpointError
v=json.load(sys.stdin)
s=CheckpointStore(Path(v['path']),EncryptionService(v['key']),clock=lambda:1000.0)
try:
 c=s.claim(v['handle']);s.release(c);print('unexpected_claim')
except CheckpointError as e:print(str(e))
"""
    payload = json.dumps({"path": str(path), "key": crypto._key, "handle": handle})
    result = subprocess.run(
        [sys.executable, "-c", program], input=payload.encode(), capture_output=True, timeout=10
    )
    assert result.returncode == 0
    assert result.stdout.decode().splitlines()[-1] == "busy"
    assert "private-" not in (result.stdout + result.stderr).decode()
    assert crypto._key not in (result.stdout + result.stderr).decode()
    store.release(owner)


def test_store_uses_full_synchronous_commits_without_changing_global_engine(tmp_path):
    store, _, _, _, _ = setup_store(tmp_path)
    with store._connection() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250


def test_database_contention_is_bounded_and_does_not_advance_state(tmp_path):
    import time

    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, path, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    with sqlite3.connect(path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
            store.claim(handle)
        assert time.monotonic() - started < 2
        blocker.rollback()
    assert store.snapshot(handle).requests == 0
    owner = store.claim(handle)
    store.release(owner)


def test_usage_only_records_keep_private_model_scopes_after_restart(tmp_path):
    from app.services.claude_cloud_ledger import usage_only_record
    from scripts.audit_claude_usage import audit_messages

    store, state, _, _, _ = setup_store(tmp_path)
    handle = store.create(state)
    owner = store.claim(handle)
    raw = {
        "type": "result",
        "subtype": "success",
        "usage": None,
        "result": "private-transcript",
        "modelUsage": {
            "private-model": {
                "inputTokens": 3,
                "outputTokens": 4,
                "cacheReadInputTokens": 5,
                "cacheCreationInputTokens": 6,
            }
        },
    }
    reduced = usage_only_record(raw, bytes.fromhex(state.job_key))
    owner.state.history_events = 1
    owner.state.sequence.invalid = 1
    owner.state.usage_records.append(reduced)
    store.save(owner)
    store.release(owner)
    restored = store.snapshot(handle)
    records = [item.model_dump() for item in restored.usage_records]
    assert "private-" not in str(records)
    assert len(records[0]["modelUsage"]) == 1
    assert audit_messages(records)["result"]["last_model_usage_observation"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 6,
    }


@pytest.mark.parametrize(
    "mutation", ["complete_with_intent", "complete_in_listing", "too_many_gaps", "model_overflow"]
)
def test_inconsistent_checkpoint_cannot_claim_completed_or_valid_usage(tmp_path, mutation):
    from pydantic import ValidationError

    from app.models.claude_cloud_verification import VerificationState

    _, state, _, _, _ = setup_store(tmp_path)
    value = state.model_dump()
    if mutation in ("complete_with_intent", "complete_in_listing"):
        value.update(
            list_complete=True,
            history_complete=True,
            outcome="complete",
            selected_session="session-fixture",
        )
        value["phase"] = "complete" if mutation == "complete_with_intent" else "listing"
        value["in_flight"] = mutation == "complete_with_intent"
    elif mutation == "too_many_gaps":
        value["sequence"]["gaps"] = 1
    else:
        value["history_events"] = 1
        value["sequence"]["invalid"] = 1
        model = {
            "inputTokens": 2**62,
            "outputTokens": 1,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
        }
        value["usage_records"] = [
            {
                "type": "result",
                "subtype": "unclassified",
                "usage": None,
                "modelUsage": {"a" * 64: model, "b" * 64: model},
            }
        ]
    with pytest.raises(ValidationError):
        VerificationState.model_validate(value)


def test_clock_rollback_cannot_extend_the_job_deadline(tmp_path):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store, state, _, _, clock = setup_store(tmp_path)
    handle = store.create(state)
    clock[0] = 999.0
    with pytest.raises(CheckpointError, match="^expired$"):
        store.claim(handle)
    assert store.snapshot(handle).outcome == "expired"
