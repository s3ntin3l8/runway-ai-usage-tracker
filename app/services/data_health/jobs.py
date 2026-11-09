"""In-process job registry for Data Health scans and fixes.

Single-flight by design: `uvicorn.run(app, ...)` (see `app/main.py`) never
passes `workers=`, so this process is the only writer, and an
`asyncio.Lock` is enough to serialize scans and applies against each other
— a rescan started while an apply is mid-flight would otherwise read a
half-committed database. A multi-worker deployment would need a
cross-process lock instead; this module does not support that topology.

Every `detect`/`apply` call runs inside `asyncio.to_thread` with its own
freshly-opened `Session(engine)` — never a request-scoped session — since a
`Session` is not safe to share across threads and a fix can run long enough
that holding a request's session open for it would be wasteful. `apply`'s
returned async hooks (e.g. `config_rekey`'s token_cache move) are awaited
back on the event loop after the thread returns, since they touch loop-bound
state (`collector_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.core.db import engine
from app.core.log_redaction import scrub_pii
from app.services import audit_log
from app.services.data_health.base import AsyncHook, Check, CheckReport, FixResult
from app.services.data_health.registry import REGISTRY, get_check
from app.services.maintenance.retry import is_sqlite_locked_or_busy

logger = logging.getLogger(__name__)

JobStatus = Literal["running", "succeeded", "failed"]

#: What kind of failure a fix ended in. `db_busy` is the one the operator can
#: act on: the database was busy with other writes, the progress so far was
#: saved, and the fix can simply be run again.
ErrorKind = Literal["db_busy", "unknown"]

_DB_BUSY_MESSAGE = "The database was busy with other writes. Progress so far was saved — run Fix again to continue."
_UNKNOWN_MESSAGE = "The fix failed. The audit log has the details."

#: How much of the failure detail is kept in the log line and the audit
#: payload. The detail is what an operator needs to diagnose the failure; it
#: is not what the dialog shows.
_DETAIL_LIMIT = 500

#: How long a check's own refusal may be. It is shown in the dialog, so it
#: stays the length of a sentence.
_MESSAGE_LIMIT = 200


class CheckBlockedError(ValueError):
    """Raised when an apply is requested for a check whose report is
    currently blocked by an upstream check's unresolved findings."""


class JobAlreadyRunningError(RuntimeError):
    """Raised when an apply is requested while another job is in flight."""


class NoScanYetError(RuntimeError):
    """Raised when an apply is requested before any scan has completed."""


class ScanFailedError(RuntimeError):
    """Raised when the latest scan failed and the cached findings are stale."""


def _classify(exc: Exception) -> tuple[ErrorKind, str]:
    """The failure's kind and the plain-language sentence the operator sees.

    A `ValueError` is a check's own refusal — "Confirm the stranded rows are
    stale before deleting them" — written for this dialog, so its message is
    what the operator is shown (the preview endpoint surfaces the same
    messages as a 400).

    Every other failure gets a plain sentence of its own: the exception's text
    never reaches the dialog, because a SQLAlchemy error renders the whole
    `UPDATE` statement and its bound parameters. The detail lives in the log
    and the audit payload instead.
    """
    if isinstance(exc, ValueError):
        # An empty refusal is still a refusal: the generic sentence is the
        # floor, so the dialog never shows an empty reason.
        return "unknown", _safe_detail(exc, limit=_MESSAGE_LIMIT) or _UNKNOWN_MESSAGE
    if isinstance(exc, OperationalError) and is_sqlite_locked_or_busy(exc):
        return "db_busy", _DB_BUSY_MESSAGE
    return "unknown", _UNKNOWN_MESSAGE


def _safe_detail(exc: Exception, *, limit: int = _DETAIL_LIMIT) -> str:
    """The failure detail without the SQL statement or its bound parameters.

    SQLAlchemy renders both as `[SQL: ...]` and `[parameters: ...]` sections
    of the exception's text. They are cut before the text is truncated, so a
    bound value can never reach the log or the audit payload.
    """
    text = str(exc)
    for marker in ("[SQL:", "[parameters:"):
        index = text.find(marker)
        if index != -1:
            text = text[:index]
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


@dataclass
class JobRecord:
    id: str
    check_id: str
    group_key: str
    params: dict[str, Any]
    status: JobStatus = "running"
    result: FixResult | None = None
    #: The plain-language reason the fix failed — never the exception's text.
    error: str | None = None
    #: What kind of failure it was: "db_busy" (the operator can run it again)
    #: or "unknown".
    error_kind: ErrorKind | None = None
    #: Whether running the same fix again could succeed.
    retryable: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None


def _scan_sync() -> dict[str, CheckReport]:
    reports: dict[str, CheckReport] = {}
    with Session(engine) as session:
        for check in REGISTRY:
            reports[check.id] = check.detect(session)
    for check in REGISTRY:
        report = reports[check.id]
        report.blocked_by = [
            dep for dep in check.blocked_by if reports.get(dep) and reports[dep].total_count > 0
        ]
    return reports


class DataHealthJobs:
    def __init__(self) -> None:
        self._apply_lock = asyncio.Lock()
        self._report_cache: dict[str, CheckReport] | None = None
        self.scan_error: str | None = None
        self.last_scanned_at: datetime | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._jobs: dict[str, JobRecord] = {}
        self._job_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    @property
    def busy(self) -> bool:
        return self._apply_lock.locked()

    def cached_report(self) -> dict[str, CheckReport] | None:
        return self._report_cache

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def wait_for_job(self, job_id: str) -> JobRecord:
        """Await a started job's background task to completion. Used by
        tests that need deterministic synchronization; the API layer polls
        `get_job` instead, since a real fix can run for a while."""
        task = self._job_tasks.get(job_id)
        if task is not None:
            # _run_apply returns None; gather (rather than a bare `await
            # task`) is used purely so this reads as a call with an effect,
            # not a no-op name reference, to static analysis.
            await asyncio.gather(task)
        record = self._jobs[job_id]
        return record

    def trigger_rescan(self) -> bool:
        """Start a scan if one isn't already running. Returns whether a new
        scan was started (idempotent — a caller racing an in-flight scan
        just observes it complete via `cached_report()`/`scanning`)."""
        if self.scanning:
            return False
        self._scan_task = asyncio.create_task(self._run_scan())
        return True

    async def _run_scan(self) -> None:
        async with self._apply_lock:  # never overlaps a running apply
            try:
                self._report_cache = await asyncio.to_thread(_scan_sync)
                self.last_scanned_at = datetime.now(UTC)
                self.scan_error = None
            except Exception as exc:  # noqa: BLE001 — retain stale report, surface scan failure
                self.scan_error = str(exc)
                logger.exception("Data Health scan failed")

    async def wait_for_scan(self) -> dict[str, CheckReport]:
        self.trigger_rescan()
        assert self._scan_task is not None
        await self._scan_task
        if self.scan_error:
            raise ScanFailedError(self.scan_error)
        assert self._report_cache is not None
        return self._report_cache

    async def start_apply(
        self,
        *,
        check_id: str,
        group_key: str,
        params: dict[str, Any],
        actor: str,
        actor_ip: str | None,
    ) -> str:
        """Validates against the cached report, then starts the apply as a
        background task and returns its job id immediately. Raises
        `NoScanYetError`, `CheckBlockedError`, or `JobAlreadyRunningError`
        without starting anything.
        """
        check = get_check(check_id)  # KeyError if unknown — caller maps to 404
        if self.scan_error:
            raise ScanFailedError(f"latest Data Health scan failed: {self.scan_error}")
        if self.scanning and self._report_cache is not None:
            raise JobAlreadyRunningError("a Data Health scan is still running")
        if self._report_cache is None:
            raise NoScanYetError("no Data Health scan has completed yet — call rescan first")
        report = self._report_cache.get(check_id)
        if report is not None and report.blocked_by:
            raise CheckBlockedError(
                f"{check_id!r} is blocked by unresolved findings in {report.blocked_by}"
            )
        if self._apply_lock.locked():
            raise JobAlreadyRunningError("a data health job is already running")

        # No `await` between the `.locked()` check above and this acquire —
        # asyncio is single-threaded and cooperative, and `Lock.acquire()`
        # on an unlocked lock returns without yielding, so nothing else can
        # interleave and steal the lock in between.
        await self._apply_lock.acquire()

        job_id = str(uuid.uuid4())
        record = JobRecord(id=job_id, check_id=check_id, group_key=group_key, params=params)
        self._jobs[job_id] = record

        self._job_tasks[job_id] = asyncio.create_task(
            self._run_apply(record, check, actor, actor_ip)
        )
        return job_id

    async def _run_apply(
        self, record: JobRecord, check: Check, actor: str, actor_ip: str | None
    ) -> None:
        try:
            result, hooks = await asyncio.to_thread(
                self._apply_sync, check, record.group_key, record.params
            )
            for hook in hooks:
                await hook()
            record.result = result
            record.status = "succeeded"
            await asyncio.to_thread(
                self._audit,
                actor=actor,
                actor_ip=actor_ip,
                action="data_health.fix_applied",
                target_id=f"{record.check_id}:{record.group_key}",
                payload={"params": record.params, "result": result.counts},
            )
        except Exception as exc:  # noqa: BLE001 — recorded on the job + audit log, not swallowed
            record.status = "failed"
            kind, message = _classify(exc)
            record.error = message
            record.error_kind = kind
            record.retryable = kind == "db_busy"
            # The detail keeps the SQL and the bound parameters out of it; the
            # operator gets it from the log and the audit payload, not from the
            # dialog.
            detail = _safe_detail(exc)
            logger.warning(
                "Data health apply failed for %s/%s (%s): %s",
                record.check_id,
                record.group_key,
                kind,
                scrub_pii(detail),
            )
            await asyncio.to_thread(
                self._audit,
                actor=actor,
                actor_ip=actor_ip,
                action="data_health.fix_failed",
                target_id=f"{record.check_id}:{record.group_key}",
                payload={"params": record.params, "error": detail, "error_kind": kind},
            )
        finally:
            record.finished_at = datetime.now(UTC)
            self._apply_lock.release()
            self.trigger_rescan()

    @staticmethod
    def _apply_sync(
        check: Check, group_key: str, params: dict[str, Any]
    ) -> tuple[FixResult, list[AsyncHook]]:
        with Session(engine) as session:
            return check.apply(session, group_key, params)

    @staticmethod
    def _audit(
        *, actor: str, actor_ip: str | None, action: str, target_id: str, payload: dict[str, Any]
    ) -> None:
        with Session(engine) as session:
            audit_log.record_as(
                session,
                actor=actor,
                actor_type="admin",
                source_ip=actor_ip,
                action=action,
                target_id=target_id,
                payload=payload,
            )


jobs = DataHealthJobs()
