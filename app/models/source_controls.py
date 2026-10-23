"""Additive Fleet source protocol; strict booleans never grant by coercion."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class ScraperReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=160)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: StrictInt = Field(ge=0)
    read: StrictBool
    state: Literal[
        "Off",
        "Setup required",
        "Connecting",
        "Ready",
        "Limited",
        "Action required",
        "Withdrawal pending",
    ]
    renewal_requested: StrictBool = False
    renewal_supported: StrictBool = False
    new: StrictBool = False
    last_read_at: float | None = Field(default=None, ge=0, le=253402300799)
    last_renewed_at: float | None = Field(default=None, ge=0)


class SourceAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictInt = Field(gt=0)
    status: Literal["applied", "denied", "superseded"]


class SourceSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal[1] = 1
    sources: list[ScraperReport] = Field(default_factory=list, max_length=128)
    acknowledgments: list[SourceAcknowledgment] = Field(default_factory=list, max_length=256)


class SourceCommandRequest(BaseModel):
    action: Literal["pause", "resume", "revoke", "request_setup", "dismiss"]


class FleetSourceStatus(ScraperReport):
    remote_paused: bool
    revoked: bool
    report_at: str
    report_stale: bool
    announcement: bool
    renewal_capable: bool
    collection_health: Literal["withdrawn", "paused", "healthy", "action_required", "untried"]
    last_collection_at: str | None


class FleetSourceCommand(BaseModel):
    id: int
    source_id: str
    generation: int
    action: Literal["pause", "resume", "revoke", "request_setup", "dismiss"]


class FleetSourceResponse(BaseModel):
    protocol: Literal[1] = 1
    sources: list[FleetSourceStatus]
    commands: list[FleetSourceCommand]
