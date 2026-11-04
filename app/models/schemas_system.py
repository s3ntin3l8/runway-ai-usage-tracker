from typing import Any

from pydantic import BaseModel, Field


class SidecarDownloadAsset(BaseModel):
    """One downloadable sidecar build on a GitHub release."""

    platform: str  # macOS | Windows | Linux | Linux-CLI
    kind: str  # "installer" (.dmg / -setup.exe) | "payload" (portable .zip / .tar.gz)
    name: str
    url: str
    size: int | None = None
    sha256_url: str | None = None


class SidecarDownloadsResponse(BaseModel):
    """Sidecar builds for one release channel, for the Fleet page's download card."""

    channel: str  # "stable" | "beta" | "edge"
    version: str | None = None  # versioned release tag or "edge"
    published_at: str | None = None
    release_url: str
    checksums_url: str | None = None  # SHA256SUMS.txt
    assets: list[SidecarDownloadAsset] = Field(default_factory=list)
    error: str | None = None  # set (with empty assets) when GitHub was unreachable


class DataHealthFinding(BaseModel):
    """One secret-safe sample row within a finding group."""

    label: str
    detail: dict[str, Any] = Field(default_factory=dict)


class DataHealthParamSpec(BaseModel):
    """One parameter a group's preview/apply call needs."""

    name: str
    label: str
    required: bool = True
    options: list[str] | None = None


class DataHealthFindingGroup(BaseModel):
    """One independently-fixable slice of a check's findings."""

    key: str
    label: str
    count: int
    fixable: bool
    not_fixable_reason: str | None = None
    params: list[DataHealthParamSpec] = Field(default_factory=list)
    samples: list[DataHealthFinding] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class DataHealthCheckReport(BaseModel):
    """One check's live findings, as of the last scan."""

    check_id: str
    title: str
    description: str
    impact: str
    recommended_action: str
    severity: str  # "error" | "warn" | "info"
    total_count: int
    fixable_count: int
    groups: list[DataHealthFindingGroup] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    blocked_by_titles: list[str] = Field(default_factory=list)
    blocked: bool


class DataHealthReportResponse(BaseModel):
    """`GET /system/data-health` — the full report, or a pending-scan marker."""

    scanning: bool
    checks: list[DataHealthCheckReport] = Field(default_factory=list)
    scan_error: str | None = None
    last_scanned_at: str | None = None


class DataHealthFixRequest(BaseModel):
    """Body for both `/preview` and `/apply` — `apply` additionally requires
    `confirm: true`."""

    group_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class DataHealthFixPlanResponse(BaseModel):
    """`POST /system/data-health/{check_id}/preview` — read-only."""

    check_id: str
    group_key: str
    summary: str
    counts: dict[str, Any] = Field(default_factory=dict)
    samples: list[DataHealthFinding] = Field(default_factory=list)
    confirmation_text: str | None = None


class DataHealthJobStartedResponse(BaseModel):
    """`POST /system/data-health/{check_id}/apply` — 202, poll `job_id`."""

    job_id: str


class DataHealthFixResultSchema(BaseModel):
    check_id: str
    group_key: str
    summary: str
    counts: dict[str, Any] = Field(default_factory=dict)


class DataHealthJobStatusResponse(BaseModel):
    """`GET /system/data-health/jobs/{job_id}`."""

    id: str
    check_id: str
    group_key: str
    status: str  # "running" | "succeeded" | "failed"
    result: DataHealthFixResultSchema | None = None
    #: The plain-language reason the fix failed — never the raw exception.
    error: str | None = None
    #: What kind of failure it was: "db_busy" or "unknown".
    error_kind: str | None = None
    #: Whether running the same fix again could succeed.
    retryable: bool = False
    started_at: str
    finished_at: str | None = None
