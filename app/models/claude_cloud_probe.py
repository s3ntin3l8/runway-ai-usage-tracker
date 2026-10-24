"""Strict operator inputs and sanitized evidence for the cloud diagnostic."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from scripts.claude_cloud_structure import (
    StructureCategory,
    StructureField,
    StructureLocation,
    StructureType,
)


class ClaudeCloudProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=320)
    source_id: str = Field(min_length=1, max_length=4096)
    organization_id: str | None = Field(
        default=None,
        pattern=r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$",
    )
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    max_history_pages: StrictInt = Field(default=2, ge=1, le=3)
    report_version: StrictInt = Field(default=1, ge=1, le=2)


class CloudUsageObservation(BaseModel):
    location: Literal["event", "data", "content"]
    report: dict[str, Any]


class _ClaudeCloudProbeFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal[
        "sample_complete",
        "sample_limited",
        "sample_not_found",
        "no_sessions",
        "busy",
        "source_unavailable",
        "source_ineligible",
        "unsupported_source",
        "source_changed",
        "expired",
        "identity_mismatch",
        "identity_unavailable",
        "rejected",
        "rate_limited",
        "unavailable",
        "timeout",
        "response_limit",
        "unsupported_contract",
    ]
    stage: Literal["source", "profile", "sessions", "history", "complete"]
    requests: int = 0
    http_status: int | None = None
    account_verified: bool = False
    organization_verified: bool = False
    session_list_records: int = 0
    session_list_complete: bool | None = None
    history_pages: int = 0
    history_events: int = 0
    history_complete: bool | None = None
    sequence: dict[Literal["numeric", "invalid", "duplicates", "gaps", "out_of_order"], int]
    observations: list[CloudUsageObservation] = Field(default_factory=list)
    cloud_contract_verified: Literal[False] = False
    ready_for_ingestion: Literal[False] = False


class ClaudeCloudProbeReport(_ClaudeCloudProbeFields):
    format_version: Literal[1] = 1


class CloudStructureLocationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    types: dict[StructureType, int]
    categories: dict[StructureCategory, int]
    fields: dict[StructureField, int]


class CloudStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    events: StrictInt = Field(ge=0, le=150)
    locations: dict[StructureLocation, CloudStructureLocationCounts]


class ClaudeCloudProbeReportV2(_ClaudeCloudProbeFields):
    format_version: Literal[2] = 2
    structure: CloudStructure
