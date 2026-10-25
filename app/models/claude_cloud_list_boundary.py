"""Strict, value-free observations for an explicit failed-list diagnostic."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.models.claude_cloud_verification import Handle
from scripts.claude_cloud_list_boundary import KINDS

Count = Annotated[StrictInt, Field(ge=0, lt=2**63)]
SmallCount = Annotated[StrictInt, Field(ge=0, le=20)]
Kind = Literal["missing", "null", "boolean", "number", "string", "array", "object", "not_object"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListBoundaryRequest(_Strict):
    job_handle: Handle


class ListBoundaryObservation(_Strict):
    top_level_kind: Kind
    data_kind: Kind
    record_count: Count | None
    records_over_limit: bool
    checked_records: SmallCount
    id_kinds: dict[Kind, SmallCount]
    valid_ids: SmallCount
    invalid_ids: SmallCount
    duplicate_valid_ids: SmallCount
    previously_seen_ids: SmallCount
    cursor_state: Literal["missing", "null", "empty_string", "nonempty_string", "invalid"]
    cursor_supported: bool
    cursor_repeated: bool | None
    has_more_state: Literal["missing", "true", "false", "invalid"]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        records = self.record_count
        if (
            set(self.id_kinds) != set(KINDS)
            or sum(self.id_kinds.values()) != self.checked_records
            or self.checked_records != (min(records, 20) if records is not None else 0)
            or (self.data_kind == "array") != (records is not None)
            or self.records_over_limit != (records is not None and records > 20)
            or self.valid_ids + self.invalid_ids != self.checked_records
            or self.valid_ids > self.id_kinds["string"]
            or self.duplicate_valid_ids > max(0, self.valid_ids - 1)
            or self.previously_seen_ids > self.valid_ids
            or self.cursor_supported != (self.cursor_state in ("null", "nonempty_string"))
            or (self.cursor_repeated is not None) != (self.cursor_state == "nonempty_string")
            or (self.top_level_kind != "object" and self.data_kind != "not_object")
        ):
            raise ValueError("invalid_boundary_report")
        return self


class ListBoundaryReport(_Strict):
    format_version: Literal[1] = 1
    outcome: Literal[
        "observed",
        "busy",
        "checkpoint_unavailable",
        "checkpoint_ineligible",
        "checkpoint_changed",
        "source_changed",
        "identity_mismatch",
        "identity_unavailable",
        "expired",
        "rejected",
        "rate_limited",
        "timeout",
        "unavailable",
        "unsupported_contract",
        "response_limit",
    ]
    stage: Literal["checkpoint", "source", "profile", "sessions", "complete"]
    requests: Annotated[StrictInt, Field(ge=0, le=2)]
    http_status: Annotated[StrictInt, Field(ge=100, le=599)] | None
    account_verified: bool
    organization_verified: bool
    original_list_pages: Annotated[StrictInt, Field(ge=0, le=512)]
    original_listed_sessions: Annotated[StrictInt, Field(ge=0, le=20000)]
    boundary: ListBoundaryObservation | None
    response_encoding: Literal["unknown", "identity", "unsupported"]
    json_state: Literal[
        "not_attempted",
        "decoded",
        "duplicate_keys",
        "nonfinite_values",
        "invalid_json",
        "encoding_error",
        "nesting_limit",
    ]
    duplicate_json_keys: Count
    nonfinite_json_values: Count
    cloud_contract_verified: Literal[False] = False
    ready_for_ingestion: Literal[False] = False
    live_acceptance_passed: Literal[False] = False
    provenance_verified: Literal[False] = False

    @model_validator(mode="after")
    def observed_only(self) -> Self:
        if (
            (
                self.json_state in ("not_attempted", "decoded")
                and (self.duplicate_json_keys or self.nonfinite_json_values)
            )
            or (self.json_state != "not_attempted" and self.response_encoding != "identity")
            or (self.json_state == "duplicate_keys" and not self.duplicate_json_keys)
            or (self.json_state == "nonfinite_values" and not self.nonfinite_json_values)
        ):
            raise ValueError("invalid_boundary_report")
        if (self.boundary is not None) != (self.outcome == "observed") or (
            self.outcome == "observed"
            and not (
                self.stage == "complete"
                and self.requests == 2
                and self.http_status == 200
                and self.account_verified
                and self.organization_verified
                and self.json_state == "decoded"
                and self.response_encoding == "identity"
            )
        ):
            raise ValueError("invalid_boundary_report")
        return self
