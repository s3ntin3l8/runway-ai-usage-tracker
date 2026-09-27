"""Tests for app/services/data_health/jobs.py — the single-flight scan/apply
job registry. Uses tiny fake `Check` instances rather than the real checks,
so these tests are about job orchestration (locking, blocking, auditing,
hook-awaiting), not check business logic.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import app.services.data_health.jobs as jobs_module
from app.models.db import AuditLog
from app.services.data_health.base import Check, CheckReport, FixResult, Severity
from app.services.data_health.jobs import (
    CheckBlockedError,
    DataHealthJobs,
    JobAlreadyRunningError,
    NoScanYetError,
)


class _FakeCheck(Check):
    def __init__(
        self,
        check_id: str,
        *,
        findings: int = 0,
        blocked_by: tuple[str, ...] = (),
        fails: bool = False,
    ):
        self.id = check_id
        self.severity = Severity.WARN
        self.blocked_by = blocked_by
        self._findings = findings
        self._fails = fails
        self.hooks: list = []
        self.apply_calls: list[tuple[str, dict]] = []

    def detect(self, session):
        return CheckReport(
            check_id=self.id, severity=self.severity, total_count=self._findings, groups=[]
        )

    def apply(self, session, group_key, params):
        self.apply_calls.append((group_key, params))
        if self._fails:
            raise RuntimeError("boom")
        return (
            FixResult(check_id=self.id, group_key=group_key, summary="done", counts={"n": 1}),
            list(self.hooks),
        )


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def checks():
    """`a` has findings and blocks `b`; `c` fails on apply."""
    return {
        "a": _FakeCheck("a", findings=1),
        "b": _FakeCheck("b", findings=0, blocked_by=("a",)),
        "c": _FakeCheck("c", findings=1, fails=True),
    }


@pytest.fixture
def jobs(monkeypatch, checks):
    monkeypatch.setattr(jobs_module, "REGISTRY", list(checks.values()))
    monkeypatch.setattr(jobs_module, "get_check", lambda check_id: checks[check_id])
    monkeypatch.setattr(jobs_module, "engine", _engine())
    return DataHealthJobs()


async def test_no_scan_yet_report_is_none(jobs):
    assert jobs.cached_report() is None


async def test_wait_for_scan_populates_the_cache(jobs):
    report = await jobs.wait_for_scan()
    assert report["a"].total_count == 1


async def test_scan_computes_blocked_by_from_other_checks_live_findings(jobs):
    report = await jobs.wait_for_scan()
    assert report["b"].blocked_by == ["a"]  # a currently has findings
    assert report["a"].blocked_by == []


async def test_trigger_rescan_is_idempotent_while_already_running(jobs):
    started_first = jobs.trigger_rescan()
    started_second = jobs.trigger_rescan()
    assert started_first is True
    assert started_second is False
    await jobs.wait_for_scan()


async def test_start_apply_before_any_scan_raises(jobs):
    with pytest.raises(NoScanYetError):
        await jobs.start_apply(check_id="a", group_key="x", params={}, actor="t", actor_ip=None)


async def test_start_apply_on_a_blocked_check_raises(jobs):
    await jobs.wait_for_scan()
    with pytest.raises(CheckBlockedError):
        await jobs.start_apply(check_id="b", group_key="x", params={}, actor="t", actor_ip=None)


async def test_start_apply_unknown_check_id_raises(jobs):
    await jobs.wait_for_scan()
    with pytest.raises(KeyError):
        await jobs.start_apply(
            check_id="nonexistent", group_key="x", params={}, actor="t", actor_ip=None
        )


async def test_start_apply_runs_and_succeeds(jobs, checks):
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="a", group_key="g1", params={"p": 1}, actor="tester", actor_ip="1.2.3.4"
    )
    record = await jobs.wait_for_job(job_id)

    assert record.status == "succeeded"
    assert record.result.counts == {"n": 1}
    assert checks["a"].apply_calls == [("g1", {"p": 1})]


async def test_start_apply_writes_a_fix_applied_audit_row(jobs):
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="a", group_key="g1", params={}, actor="tester", actor_ip="1.2.3.4"
    )
    await jobs.wait_for_job(job_id)

    with Session(jobs_module.engine) as session:
        rows = list(session.exec(select(AuditLog)))
    assert any(r.action == "data_health.fix_applied" and r.actor == "tester" for r in rows)


async def test_second_apply_while_one_is_running_raises(jobs):
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="a", group_key="g1", params={}, actor="t", actor_ip=None
    )
    with pytest.raises(JobAlreadyRunningError):
        await jobs.start_apply(check_id="a", group_key="g2", params={}, actor="t", actor_ip=None)

    await jobs.wait_for_job(job_id)  # drain so the fixture's engine isn't used after teardown


async def test_apply_failure_records_failed_status_and_audit(jobs):
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="c", group_key="g1", params={}, actor="t", actor_ip=None
    )
    record = await jobs.wait_for_job(job_id)

    assert record.status == "failed"
    assert "boom" in (record.error or "")
    with Session(jobs_module.engine) as session:
        rows = list(session.exec(select(AuditLog)))
    assert any(r.action == "data_health.fix_failed" for r in rows)


async def test_apply_triggers_a_rescan_after_completing(jobs):
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="a", group_key="g1", params={}, actor="t", actor_ip=None
    )
    await jobs.wait_for_job(job_id)
    # the apply's finally clause starts a rescan task — wait for it too.
    if jobs.scanning:
        assert jobs._scan_task is not None
        await jobs._scan_task

    assert jobs.cached_report() is not None


async def test_apply_awaits_returned_hooks(jobs, checks):
    calls: list[str] = []

    async def hook() -> None:
        calls.append("ran")

    checks["a"].hooks = [hook]
    await jobs.wait_for_scan()

    job_id = await jobs.start_apply(
        check_id="a", group_key="g1", params={}, actor="t", actor_ip=None
    )
    await jobs.wait_for_job(job_id)

    assert calls == ["ran"]
