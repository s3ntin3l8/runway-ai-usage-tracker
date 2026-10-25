"""Encrypted, revision-bound SQLite checkpoints for one explicit verification job.

No network operations run in this module or while its short transactions hold a
write lock. Separate FULL-synchronous connections durably commit request intents.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import quote

from app.core.encryption import EncryptionService
from app.models.claude_cloud_verification import CheckpointEnvelope, VerificationState

MAX_PLAINTEXT = 16 * 1024 * 1024
LEASE_SECONDS = 45


MAX_ARCHIVES = 64


class CheckpointError(ValueError):
    """Only fixed categories cross the storage boundary."""


@dataclass
class Claim:
    handle: str
    owner: str
    revision: int
    state: VerificationState


class CheckpointStore:
    def __init__(
        self, database: Path, crypto: EncryptionService, *, clock: Callable[[], float] = time.time
    ):
        self.database = database
        self.crypto = crypto
        self.clock = clock

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(
                "file:" + quote(str(self.database.resolve())) + "?mode=rw",
                uri=True,
                timeout=0.25,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=250")
            yield connection
        except sqlite3.Error:
            raise CheckpointError("checkpoint_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    def _require_encryption(self) -> None:
        if not self.crypto.is_enabled:
            raise CheckpointError("encryption_required")

    @staticmethod
    def _checked(state: VerificationState) -> VerificationState:
        try:
            # Disable value-bearing serializer warnings; validate additions ourselves.
            return VerificationState.model_validate(state.model_dump(warnings="none"))
        except Exception:
            raise CheckpointError("checkpoint_unavailable") from None

    def _encode(self, handle: str, revision: int, state: VerificationState) -> str:
        self._require_encryption()
        try:
            # Revalidate nested mutable lists; never serialize unchecked additions.
            checked = self._checked(state)
            envelope = CheckpointEnvelope(handle=handle, revision=revision, state=checked)
            plaintext = envelope.model_dump_json()
            if len(plaintext.encode()) > MAX_PLAINTEXT:
                raise ValueError()
            ciphertext = self.crypto.encrypt_string(plaintext)
            if not ciphertext.startswith("gAAAAA"):
                raise ValueError()
            return ciphertext
        except Exception:
            raise CheckpointError("checkpoint_unavailable") from None

    def _decode(self, row: sqlite3.Row) -> VerificationState:
        self._require_encryption()
        try:
            ciphertext = row["state_encrypted"]
            if not isinstance(ciphertext, str) or not ciphertext.startswith("gAAAAA"):
                raise ValueError()
            if len(ciphertext) > MAX_PLAINTEXT * 2:
                raise ValueError()
            plaintext = self.crypto.decrypt_string(ciphertext)
            if len(plaintext.encode()) > MAX_PLAINTEXT:
                raise ValueError()
            envelope = CheckpointEnvelope.model_validate_json(plaintext)
            if envelope.handle != row["job_handle"] or envelope.revision != row["revision"]:
                raise ValueError()
            return envelope.state
        except Exception:
            raise CheckpointError("checkpoint_unavailable") from None

    def _row(self, handle: str) -> sqlite3.Row:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM claude_cloud_verification WHERE id=1 AND job_handle=?", (handle,)
            ).fetchone()
        if row is None:
            raise CheckpointError("not_found")
        return row

    def current_handle(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT job_handle FROM claude_cloud_verification WHERE id=1"
            ).fetchone()
        return row["job_handle"] if row is not None else None

    def create(self, state: VerificationState) -> str:
        handle = secrets.token_hex(16)
        encrypted = self._encode(handle, 0, state)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM claude_cloud_verification WHERE id=1").fetchone():
                raise CheckpointError("job_exists")
            if connection.execute(
                "SELECT job_handle FROM claude_cloud_verification_archives WHERE job_handle=?",
                (handle,),
            ).fetchone():
                raise CheckpointError("checkpoint_unavailable")
            connection.execute(
                "INSERT INTO claude_cloud_verification "
                "(id,job_handle,revision,state_encrypted,lease_owner,lease_expires) "
                "VALUES (1,?,0,?,NULL,0)",
                (handle, encrypted),
            )
            connection.commit()
        return handle

    def _archived_row(self, handle: str) -> sqlite3.Row:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM claude_cloud_verification_archives WHERE job_handle=?", (handle,)
            ).fetchone()
        if row is None:
            raise CheckpointError("not_found")
        return row

    def _decode_archive(self, row: sqlite3.Row) -> VerificationState:
        state = self._decode(row)
        if state.phase not in ("complete", "stopped"):
            raise CheckpointError("checkpoint_unavailable")
        return state

    def archived_snapshot(self, handle: str) -> VerificationState:
        """Local recovery only; never project historical usage without source authority."""
        return self._decode_archive(self._archived_row(handle))

    def archive_preview(self, handle: str) -> dict[str, str | int]:
        """Inert administrative metadata; no provider or source credential operations."""
        self._require_encryption()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM claude_cloud_verification_archives WHERE job_handle=?", (handle,)
            ).fetchone()
            if row is not None:
                self._decode_archive(row)
                return {"state": "archived", "job_handle": handle, "revision": row["revision"]}
            row = connection.execute(
                "SELECT * FROM claude_cloud_verification WHERE id=1 AND job_handle=?", (handle,)
            ).fetchone()
            if row is None:
                raise CheckpointError("not_found")
            state = self._decode(row)
            return {
                "state": "terminal" if state.phase in ("complete", "stopped") else "not_terminal",
                "job_handle": handle,
                "revision": row["revision"],
            }

    def archive(self, handle: str, revision: int) -> None:
        """Verify an unchanged encrypted terminal copy before atomically releasing its slot."""
        self._require_encryption()
        if type(revision) is not int or revision < 0:
            raise CheckpointError("checkpoint_changed")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            archived = connection.execute(
                "SELECT * FROM claude_cloud_verification_archives WHERE job_handle=?", (handle,)
            ).fetchone()
            if archived is not None:
                self._decode_archive(archived)
                if archived["revision"] != revision:
                    raise CheckpointError("checkpoint_changed")
                return  # Idempotent; never touch a newer current job.
            row = connection.execute(
                "SELECT * FROM claude_cloud_verification WHERE id=1 AND job_handle=?", (handle,)
            ).fetchone()
            if row is None:
                raise CheckpointError("not_found")
            if row["revision"] != revision:
                raise CheckpointError("checkpoint_changed")
            if row["lease_owner"] is not None and row["lease_expires"] > self.clock():
                raise CheckpointError("busy")
            state = self._decode(row)
            if state.phase not in ("complete", "stopped"):
                raise CheckpointError("not_terminal")
            count = connection.execute(
                "SELECT count(*) FROM claude_cloud_verification_archives"
            ).fetchone()[0]
            if count >= MAX_ARCHIVES:
                raise CheckpointError("archive_limit")
            connection.execute(
                "INSERT INTO claude_cloud_verification_archives "
                "(job_handle,revision,state_encrypted,archived_at) VALUES (?,?,?,?)",
                (handle, revision, row["state_encrypted"], self.clock()),
            )
            saved = connection.execute(
                "SELECT * FROM claude_cloud_verification_archives WHERE job_handle=?", (handle,)
            ).fetchone()
            if (
                saved is None
                or saved["revision"] != revision
                or saved["state_encrypted"] != row["state_encrypted"]
                or self._decode(saved) != state
            ):
                raise CheckpointError("checkpoint_unavailable")
            changed = connection.execute(
                "DELETE FROM claude_cloud_verification WHERE id=1 AND job_handle=? AND revision=?",
                (handle, revision),
            ).rowcount
            if changed != 1:
                raise CheckpointError("checkpoint_changed")
            connection.commit()

    def snapshot(self, handle: str) -> VerificationState:
        return self._decode(self._row(handle))

    def _stop(
        self, row: sqlite3.Row, state: VerificationState, reason: Literal["interrupted", "expired"]
    ) -> NoReturn:
        state.phase = "stopped"
        state.outcome = reason
        revision = row["revision"] + 1
        encrypted = self._encode(row["job_handle"], revision, state)
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE claude_cloud_verification SET revision=?,state_encrypted=?,"
                "lease_owner=NULL,lease_expires=0 WHERE id=1 AND job_handle=? AND revision=? "
                "AND lease_owner IS ?",
                (revision, encrypted, row["job_handle"], row["revision"], row["lease_owner"]),
            ).rowcount
            if changed != 1:
                raise CheckpointError("busy")
        raise CheckpointError(reason)

    def claim(self, handle: str) -> Claim:
        row = self._row(handle)
        state = self._decode(row)
        now = self.clock()
        if row["lease_owner"] is not None and row["lease_expires"] > now:
            raise CheckpointError("busy")
        if state.phase in ("complete", "stopped"):
            raise CheckpointError("terminal")
        if row["lease_owner"] is not None or state.in_flight:
            self._stop(row, state, "interrupted")
        if not state.created_at <= now < state.deadline:
            self._stop(row, state, "expired")
        owner = secrets.token_hex(16)
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE claude_cloud_verification SET lease_owner=?,lease_expires=? "
                "WHERE id=1 AND job_handle=? AND revision=? AND lease_owner IS NULL",
                (owner, now + LEASE_SECONDS, handle, row["revision"]),
            ).rowcount
            if changed != 1:
                raise CheckpointError("busy")
        return Claim(handle, owner, row["revision"], state)

    @staticmethod
    def _transition(old: VerificationState, new: VerificationState) -> None:
        immutable = (
            "account_id",
            "source_id",
            "job_key",
            "source_digest",
            "created_at",
            "deadline",
            "target_session",
            "declared_category",
            "expected_local",
        )
        counters = (
            "requests",
            "total_bytes",
            "listed_sessions",
            "list_pages",
            "history_pages",
            "history_events",
        )
        if (
            any(getattr(old, key) != getattr(new, key) for key in immutable)
            or any(getattr(new, key) < getattr(old, key) for key in counters)
            or any(
                getattr(old, key) is not None and getattr(old, key) != getattr(new, key)
                for key in ("subject", "organization", "selected_session")
            )
            or old.phase in ("complete", "stopped")
            or (old.phase == "history" and new.phase == "listing")
        ):
            raise CheckpointError("invalid_transition")

    def save(self, claim: Claim) -> None:
        row = self._row(claim.handle)
        now = self.clock()
        if (
            row["lease_owner"] != claim.owner
            or row["revision"] != claim.revision
            or row["lease_expires"] <= now
        ):
            raise CheckpointError("lost_lease")
        previous = self._decode(row)
        checked = self._checked(claim.state)
        self._transition(previous, checked)
        revision = claim.revision + 1
        encrypted = self._encode(claim.handle, revision, checked)
        with self._connection() as connection:
            changed = connection.execute(
                "UPDATE claude_cloud_verification SET revision=?,state_encrypted=?,lease_expires=? "
                "WHERE id=1 AND job_handle=? AND revision=? AND lease_owner=? AND lease_expires>?",
                (
                    revision,
                    encrypted,
                    now + LEASE_SECONDS,
                    claim.handle,
                    claim.revision,
                    claim.owner,
                    self.clock(),
                ),
            ).rowcount
            if changed != 1:
                raise CheckpointError("lost_lease")
        claim.revision = revision
        claim.state = checked

    def release(self, claim: Claim) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE claude_cloud_verification SET lease_owner=NULL,lease_expires=0 "
                "WHERE id=1 AND job_handle=? AND lease_owner=?",
                (claim.handle, claim.owner),
            )

    def cancel(self, handle: str) -> None:
        row = self._row(handle)
        if row["lease_owner"] is not None and row["lease_expires"] > self.clock():
            raise CheckpointError("busy")
        with self._connection() as connection:
            changed = connection.execute(
                "DELETE FROM claude_cloud_verification WHERE id=1 AND job_handle=? AND revision=? "
                "AND (lease_owner IS NULL OR lease_expires<=?)",
                (handle, row["revision"], self.clock()),
            ).rowcount
            if changed != 1:
                raise CheckpointError("busy")
