"""Strict operator inputs and sanitized evidence for the cloud diagnostic."""

from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from scripts.claude_cloud_structure import (
    CATEGORIES,
    FIELDS,
    LOCATION_PATHS,
    VALUE_TYPES,
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


class _CloudUsageObservationFields(BaseModel):
    report: dict[str, Any]


class CloudUsageObservation(_CloudUsageObservationFields):
    location: Literal["event", "data", "content"]


class CloudUsageObservationV2(_CloudUsageObservationFields):
    location: Literal["event", "data", "content", "payload"]


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
    cloud_contract_verified: Literal[False] = False
    ready_for_ingestion: Literal[False] = False


class ClaudeCloudProbeReport(_ClaudeCloudProbeFields):
    format_version: Literal[1] = 1
    observations: list[CloudUsageObservation] = Field(default_factory=list)


StructureCount = Annotated[StrictInt, Field(ge=0, le=150)]


class CloudStructureLocationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    types: dict[StructureType, StructureCount]
    categories: dict[StructureCategory, StructureCount]
    fields: dict[StructureField, StructureCount]

    @model_validator(mode="after")
    def complete_keys(self) -> Self:
        if (
            set(self.types) != set(VALUE_TYPES)
            or set(self.categories) != set(CATEGORIES)
            or set(self.fields) != set(FIELDS)
        ):
            raise ValueError("invalid_structure")
        return self


class CloudStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    events: StrictInt = Field(ge=0, le=150)
    locations: dict[StructureLocation, CloudStructureLocationCounts]

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if set(self.locations) != set(LOCATION_PATHS):
            raise ValueError("invalid_structure")
        for row in self.locations.values():
            objects = row.types["object"]
            if (
                any(
                    count > self.events
                    for histogram in (row.types, row.categories, row.fields)
                    for count in histogram.values()
                )
                or sum(row.types.values()) != self.events
                or sum(row.categories.values()) != self.events
                or row.categories["not_object"] != self.events - objects
                or any(count > objects for count in row.fields.values())
                or row.categories["missing"] != objects - row.fields["type"]
            ):
                raise ValueError("invalid_structure")
        if self.locations["event"].types["object"] != self.events:
            raise ValueError("invalid_structure")
        for location, path in LOCATION_PATHS.items():
            if path:
                parent = next(key for key, parts in LOCATION_PATHS.items() if parts == path[:-1])
                if (
                    self.locations[location].types["missing"]
                    != self.events - self.locations[parent].fields[cast(StructureField, path[-1])]
                ):
                    raise ValueError("invalid_structure")
        return self


class ClaudeCloudProbeReportV2(_ClaudeCloudProbeFields):
    format_version: Literal[2] = 2
    observations: list[CloudUsageObservationV2] = Field(default_factory=list)
    structure: CloudStructure

    @model_validator(mode="after")
    def matching_events(self) -> Self:
        if self.structure.events != self.history_events:
            raise ValueError("invalid_structure")
        return self
