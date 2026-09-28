"""Data Health API — `/api/v1/system/data-health/*`. Every route sits behind
`require_admin_key`; reads use a request-scoped session (`get_session`),
while a scan or an apply always opens its own session inside a worker
thread (see `app/services/data_health/jobs.py`) since that work outlives the
request.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app.core.db import get_session
from app.core.rate_limit import limiter
from app.core.security import require_admin_key
from app.models.schemas import (
    DataHealthCheckReport,
    DataHealthFinding,
    DataHealthFindingGroup,
    DataHealthFixPlanResponse,
    DataHealthFixRequest,
    DataHealthFixResultSchema,
    DataHealthJobStartedResponse,
    DataHealthJobStatusResponse,
    DataHealthParamSpec,
    DataHealthReportResponse,
)
from app.services import audit_log
from app.services.data_health.base import CheckReport, Finding, FindingGroup, ParamSpec
from app.services.data_health.jobs import (
    CheckBlockedError,
    JobAlreadyRunningError,
    NoScanYetError,
    ScanFailedError,
    jobs,
)
from app.services.data_health.registry import get_check

logger = logging.getLogger(__name__)
router = APIRouter()


def _finding_schema(finding: Finding) -> DataHealthFinding:
    return DataHealthFinding(label=finding.label, detail=finding.detail)


def _param_schema(param: ParamSpec) -> DataHealthParamSpec:
    return DataHealthParamSpec(
        name=param.name, label=param.label, required=param.required, options=param.options
    )


def _group_schema(group: FindingGroup) -> DataHealthFindingGroup:
    return DataHealthFindingGroup(
        key=group.key,
        label=group.label,
        count=group.count,
        fixable=group.fixable,
        not_fixable_reason=group.not_fixable_reason,
        params=[_param_schema(p) for p in group.params],
        samples=[_finding_schema(s) for s in group.samples],
        detail=group.detail,
    )


def _report_schema(report: CheckReport) -> DataHealthCheckReport:
    return DataHealthCheckReport(
        check_id=report.check_id,
        severity=report.severity.value,
        total_count=report.total_count,
        fixable_count=report.fixable_count,
        groups=[_group_schema(g) for g in report.groups],
        blocked_by=report.blocked_by,
        blocked=report.blocked,
    )


@router.get("/", response_model=DataHealthReportResponse)
@limiter.limit("30/minute")
async def get_report(
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> DataHealthReportResponse:
    """The cached report. Starts a scan in the background on the first call
    if none has completed yet, rather than blocking this request on it."""
    cached = jobs.cached_report()
    if cached is None and jobs.scan_error is None:
        jobs.trigger_rescan()
    if cached is None:
        return DataHealthReportResponse(
            scanning=jobs.scanning,
            checks=[],
            scan_error=jobs.scan_error,
            last_scanned_at=jobs.last_scanned_at.isoformat() if jobs.last_scanned_at else None,
        )
    return DataHealthReportResponse(
        scanning=jobs.scanning,
        checks=[_report_schema(r) for r in cached.values()],
        scan_error=jobs.scan_error,
        last_scanned_at=jobs.last_scanned_at.isoformat() if jobs.last_scanned_at else None,
    )


@router.post("/rescan", status_code=202)
@limiter.limit("6/minute")
async def rescan(
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> dict[str, bool]:
    started = jobs.trigger_rescan()
    return {"started": started}


@router.post("/{check_id}/preview", response_model=DataHealthFixPlanResponse)
@limiter.limit("20/minute")
async def preview_fix(
    check_id: str,
    body: DataHealthFixRequest,
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> DataHealthFixPlanResponse:
    try:
        check = get_check(check_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        plan = check.plan(session, body.group_key, body.params)
    except (NotImplementedError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DataHealthFixPlanResponse(
        check_id=plan.check_id,
        group_key=plan.group_key,
        summary=plan.summary,
        counts=plan.counts,
        samples=[_finding_schema(s) for s in plan.samples],
        confirmation_text=plan.confirmation_text,
    )


@router.post("/{check_id}/apply", status_code=202, response_model=DataHealthJobStartedResponse)
@limiter.limit("2/minute")
async def apply_fix(
    check_id: str,
    body: DataHealthFixRequest,
    request: Request,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_admin_key),
) -> DataHealthJobStartedResponse:
    if not body.confirm:
        raise HTTPException(status_code=400, detail="apply requires confirm: true")

    actor = audit_log.resolve_actor(request)
    actor_ip = audit_log.resolve_source_ip(request)

    try:
        job_id = await jobs.start_apply(
            check_id=check_id,
            group_key=body.group_key,
            params=body.params,
            actor=actor,
            actor_ip=actor_ip,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoScanYetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ScanFailedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (CheckBlockedError, JobAlreadyRunningError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit_log.record(
        session,
        request,
        action="data_health.fix_requested",
        target_id=f"{check_id}:{body.group_key}",
        payload={"params": body.params, "job_id": job_id},
    )
    return DataHealthJobStartedResponse(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=DataHealthJobStatusResponse)
@limiter.limit("120/minute")
async def get_job_status(
    job_id: str,
    request: Request,
    _auth: None = Depends(require_admin_key),
) -> DataHealthJobStatusResponse:
    record = jobs.get_job(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown job id: {job_id!r}")
    result = None
    if record.result is not None:
        result = DataHealthFixResultSchema(
            check_id=record.result.check_id,
            group_key=record.result.group_key,
            summary=record.result.summary,
            counts=record.result.counts,
        )
    return DataHealthJobStatusResponse(
        id=record.id,
        check_id=record.check_id,
        group_key=record.group_key,
        status=record.status,
        result=result,
        error=record.error,
        started_at=record.started_at.isoformat(),
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
    )
