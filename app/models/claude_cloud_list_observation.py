"""Shared strict, value-free metadata for rejected session-list responses."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from scripts.claude_cloud_list_boundary import KINDS

Count = Annotated[StrictInt, Field(ge=0, lt=2**63)]
SmallCount = Annotated[StrictInt, Field(ge=0, le=20)]
Kind = Literal["missing", "null", "boolean", "number", "string", "array", "object", "not_object"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListBoundaryObservation(StrictModel):
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


class RejectedListEvidence(StrictModel):
    boundary: ListBoundaryObservation | None = None
    response_encoding: Literal["unknown", "identity", "unsupported"] = "unknown"
    json_state: Literal[
        "not_attempted",
        "decoded",
        "duplicate_keys",
        "nonfinite_values",
        "invalid_json",
        "encoding_error",
        "nesting_limit",
    ] = "not_attempted"
    duplicate_json_keys: Count = 0
    nonfinite_json_values: Count = 0

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if (
            (self.boundary is not None) != (self.json_state == "decoded")
            or (self.json_state != "not_attempted" and self.response_encoding != "identity")
            or (
                self.json_state in ("not_attempted", "decoded")
                and (self.duplicate_json_keys or self.nonfinite_json_values)
            )
            or (self.json_state == "duplicate_keys" and not self.duplicate_json_keys)
            or (self.json_state == "nonfinite_values" and not self.nonfinite_json_values)
            or (self.response_encoding == "unknown")
            or ((self.json_state == "not_attempted") != (self.response_encoding == "unsupported"))
        ):
            raise ValueError("invalid_boundary_report")
        return self
