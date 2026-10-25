"""Strict, value-free observations for an explicit failed-list diagnostic."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from app.models.claude_cloud_list_observation import (
    Count,
    ListBoundaryObservation,
    _Strict,
)
from app.models.claude_cloud_verification import Handle


class ListBoundaryRequest(_Strict):
    job_handle: Handle


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
    original_list_pages: Annotated[StrictInt, Field(ge=0, le=512)] | None
    original_listed_sessions: Annotated[StrictInt, Field(ge=0, le=20000)] | None
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
        if (self.original_list_pages is None) != (self.original_listed_sessions is None) or (
            self.original_list_pages is not None
            and self.original_listed_sessions is not None
            and self.original_listed_sessions > self.original_list_pages * 20
        ):
            raise ValueError("invalid_boundary_report")
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
                self.original_list_pages is not None
                and self.stage == "complete"
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
