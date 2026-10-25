"""Strict private checkpoint shapes; never return these through a public route."""

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

Counter = Annotated[StrictInt, Field(ge=0, lt=2**63)]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Handle = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{32}$")]
SessionID = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
UUIDString = Annotated[
    StrictStr,
    Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"),
]


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, serialize_by_alias=True)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def literal_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid_checkpoint")
        return value


class UsageCounters(_PrivateModel):
    input_tokens: Counter
    output_tokens: Counter
    cache_read_input_tokens: Counter
    cache_creation_input_tokens: Counter


class ModelCounters(_PrivateModel):
    input_tokens: Counter = Field(alias="inputTokens")
    output_tokens: Counter = Field(alias="outputTokens")
    cache_read_input_tokens: Counter = Field(alias="cacheReadInputTokens")
    cache_creation_input_tokens: Counter = Field(alias="cacheCreationInputTokens")


class CheckpointMessage(_PrivateModel):
    id: Digest | None
    usage: UsageCounters | Literal[False]

    @field_validator("usage", mode="before")
    @classmethod
    def valid_unknown(cls, value: object) -> object:
        if value is not False and not isinstance(value, (dict, UsageCounters)):
            raise ValueError("invalid_usage_record")
        return value


class CheckpointContext(_PrivateModel):
    session_scope: Digest | None = None
    request_scope: Digest | None = None
    record_version: (
        Annotated[
            StrictStr,
            Field(pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,32})?$"),
        ]
        | None
    ) = None
    scope_conflict: bool = False
    scope_unknown: bool = False
    event_sequence: Counter | None = None
    sdk_timestamp: float | None = Field(default=None, ge=0, lt=253402300800, allow_inf_nan=False)
    provider_timestamp: float | None = Field(
        default=None, ge=0, lt=253402300800, allow_inf_nan=False
    )
    sdk_timestamp_unknown: bool = False
    provider_timestamp_unknown: bool = False


class CheckpointAssistant(CheckpointContext):
    type: Literal["assistant"]
    message: CheckpointMessage
    parent_tool_use_id: Digest | Literal[True] | None = None

    @field_validator("parent_tool_use_id", mode="before")
    @classmethod
    def literal_parent_flag(cls, value: object) -> object:
        if (
            value is not None
            and value is not True
            and not (isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))
        ):
            raise ValueError("invalid_usage_record")
        return value


class CheckpointSystem(CheckpointContext):
    type: Literal["system"]
    subtype: Literal["init"]


class CheckpointResult(CheckpointContext):
    type: Literal["result"]
    subtype: Literal["unclassified", "error_observed"]
    usage: UsageCounters | Literal[False] | None
    model_usage: dict[Digest, ModelCounters] | Literal[False] | None = Field(alias="modelUsage")

    @field_validator("usage", "model_usage", mode="before")
    @classmethod
    def valid_unknown(cls, value: object) -> object:
        if (
            value is not None
            and value is not False
            and not isinstance(value, (dict, UsageCounters))
        ):
            raise ValueError("invalid_usage_record")
        return value

    @model_validator(mode="after")
    def bounded_model_aggregate(self) -> Self:
        if isinstance(self.model_usage, dict) and any(
            sum(getattr(value, field) for value in self.model_usage.values()) >= 2**63
            for field in ModelCounters.model_fields
        ):
            raise ValueError("invalid_usage_record")
        return self


CheckpointRecord = Annotated[
    CheckpointAssistant | CheckpointResult | CheckpointSystem, Field(discriminator="type")
]


class SequenceCounts(_PrivateModel):
    numeric: Counter = 0
    invalid: Counter = 0
    duplicates: Counter = 0
    gaps: Counter = 0
    out_of_order: Counter = 0


VerificationOutcome = Literal[
    "pending",
    "complete",
    "cancelled",
    "interrupted",
    "expired",
    "limit_reached",
    "source_changed",
    "source_unavailable",
    "source_ineligible",
    "unsupported_source",
    "identity_mismatch",
    "identity_unavailable",
    "rejected",
    "rate_limited",
    "timeout",
    "unavailable",
    "unsupported_contract",
    "response_limit",
    "no_sessions",
    "target_not_found",
]


class VerificationState(_PrivateModel):
    version: Literal[1] = 1
    account_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    source_id: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    job_key: Digest
    source_digest: Digest
    created_at: float = Field(ge=0, allow_inf_nan=False)
    deadline: float = Field(ge=0, allow_inf_nan=False)
    subject: UUIDString | None = None
    organization: UUIDString | None = None
    target_session: SessionID | None = None
    selected_session: SessionID | None = None
    declared_category: Literal[
        "cli_code",
        "desktop_code",
        "remote_code",
        "cowork",
        "ordinary_chat",
        "mobile_only",
        "unknown",
    ] = "unknown"
    execution_kind: Literal["anthropic_cloud", "bridge", "unknown"] = "unknown"
    phase: Literal["listing", "history", "complete", "stopped"] = "listing"
    outcome: VerificationOutcome = "pending"
    in_flight: bool = False
    last_verified_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    last_http_status: Annotated[StrictInt, Field(ge=100, le=599)] | None = None
    requests: Annotated[StrictInt, Field(ge=0, le=512)] = 0
    total_bytes: Annotated[StrictInt, Field(ge=0, le=256 * 1024 * 1024)] = 0
    listed_sessions: Annotated[StrictInt, Field(ge=0, le=20000)] = 0
    list_pages: Annotated[StrictInt, Field(ge=0, le=512)] = 0
    history_pages: Annotated[StrictInt, Field(ge=0, le=512)] = 0
    history_events: Annotated[StrictInt, Field(ge=0, le=100000)] = 0
    list_complete: bool = False
    history_complete: bool = False
    next_cursor: Annotated[StrictStr, Field(min_length=1, max_length=2048)] | None = None
    seen_cursors: list[Digest] = Field(default_factory=list, max_length=512)
    seen_sessions: list[Digest] = Field(default_factory=list, max_length=20000)
    sequences: list[Counter] = Field(default_factory=list, max_length=100000)
    previous_sequence: Counter | None = None
    sequence: SequenceCounts = Field(default_factory=SequenceCounts)
    usage_records: list[CheckpointRecord] = Field(default_factory=list, max_length=100000)
    expected_local: list[CheckpointAssistant] = Field(default_factory=list, max_length=256)

    @field_validator("next_cursor")
    @classmethod
    def safe_cursor(cls, value: str | None) -> str | None:
        if value is not None:
            if any(ord(character) < 32 for character in value):
                raise ValueError("invalid_checkpoint")
            value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        known_sequences = set(self.sequences)
        if (
            not self.created_at <= self.deadline <= self.created_at + 1800
            or len(self.seen_sessions) != self.listed_sessions
            or len(set(self.seen_sessions)) != len(self.seen_sessions)
            or len(set(self.seen_cursors)) != len(self.seen_cursors)
            or self.sequence.numeric + self.sequence.invalid != self.history_events
            or len(known_sequences) != len(self.sequences)
            or len(self.sequences) + self.sequence.duplicates != self.sequence.numeric
            or len(self.usage_records) > self.history_events
            or any(
                record.event_sequence is not None and record.event_sequence not in known_sequences
                for record in self.usage_records
            )
            or self.history_pages + self.list_pages > self.requests
            or (
                self.phase == "history"
                and (self.selected_session is None or not self.list_complete)
            )
            or (self.previous_sequence is None) != (self.sequence.numeric == 0)
            or (
                self.previous_sequence is not None and self.previous_sequence not in known_sequences
            )
            or any(
                getattr(self.sequence, field) > self.history_events
                for field in ("numeric", "invalid", "duplicates", "gaps", "out_of_order")
            )
            or (
                self.phase == "complete"
                and (
                    self.in_flight
                    or not self.list_complete
                    or self.outcome not in ("complete", "no_sessions", "target_not_found")
                )
            )
            or (
                self.outcome == "complete"
                and not (
                    self.phase == "complete"
                    and self.list_complete
                    and self.history_complete
                    and self.selected_session is not None
                )
            )
        ):
            raise ValueError("invalid_checkpoint")
        return self


class CheckpointEnvelope(_PrivateModel):
    version: Literal[1] = 1
    handle: Handle
    revision: Counter
    state: VerificationState


class VerificationStart(_PrivateModel):
    action: Literal["start"] = "start"
    account_id: Annotated[StrictStr, Field(min_length=1, max_length=320)]
    source_id: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    organization_id: UUIDString | None = None
    session_id: SessionID | None = None
    job_key: Digest | None = None
    expected_local: list[CheckpointAssistant] = Field(default_factory=list, max_length=256)
    declared_category: Literal[
        "cli_code",
        "desktop_code",
        "remote_code",
        "cowork",
        "ordinary_chat",
        "mobile_only",
        "unknown",
    ] = "unknown"

    @model_validator(mode="after")
    def valid_fixture(self) -> Self:
        if self.expected_local and (
            self.job_key is None
            or any(
                row.message.id is None
                or not isinstance(row.message.usage, UsageCounters)
                or row.parent_tool_use_id is True
                for row in self.expected_local
            )
        ):
            raise ValueError("invalid_local_fixture")
        return self


class VerificationStep(_PrivateModel):
    action: Literal["step"]
    job_handle: Handle


class VerificationStatus(_PrivateModel):
    action: Literal["status"]
    job_handle: Handle | None = None


class VerificationCancel(_PrivateModel):
    action: Literal["cancel"]
    job_handle: Handle


class VerificationReport(_PrivateModel):
    """Public fixed projection; identifiers appear only as an opaque transport handle."""

    format_version: Literal[1]
    job_handle: Handle
    started_at: float
    deadline: float
    last_verified_at: float | None
    phase: Literal["listing", "history", "complete", "stopped"]
    outcome: VerificationOutcome
    requests: Counter
    accepted_response_bytes: Counter
    http_status: int | None
    account_verified: bool
    organization_verified: bool
    listed_sessions: Counter
    list_pages: Counter
    list_complete: bool
    history_pages: Counter
    history_events: Counter
    history_complete: bool
    sequence: SequenceCounts
    audit: dict[str, object]
    comparison: dict[str, object]
    subagent_comparison: dict[str, object]
    scope_observations: dict[str, Counter]
    declared_category: Literal[
        "cli_code",
        "desktop_code",
        "remote_code",
        "cowork",
        "ordinary_chat",
        "mobile_only",
        "unknown",
    ]
    execution_kind: Literal["anthropic_cloud", "bridge", "unknown"]
    cloud_contract_verified: Literal[False]
    ready_for_ingestion: Literal[False]
    live_acceptance_passed: Literal[False]
    provenance_verified: Literal[False]

    @model_validator(mode="after")
    def allowlisted_report(self) -> Self:
        from scripts.verify_claude_cloud import _history_report

        report = self.model_dump(exclude={"job_handle"})
        _history_report(report)
        return self


class VerificationEmpty(_PrivateModel):
    state: Literal["absent", "cancelled"]
