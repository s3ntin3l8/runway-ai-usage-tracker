"""Strict exact-target inputs and content-free ordinary Chat fixture observations."""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from scripts.claude_chat_evidence import COUNTER_SHAPES, FIELDS, SHAPES

UUID_PATTERN = r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
Count = Annotated[StrictInt, Field(ge=0, le=500)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChatFixtureRequest(_Strict):
    account_id: str = Field(min_length=1, max_length=320)
    source_id: str = Field(min_length=1, max_length=4096)
    organization_id: str = Field(pattern=UUID_PATTERN)
    conversation_id: str = Field(pattern=UUID_PATTERN)


class ChatUsageScope(_Strict):
    records: Count
    shapes: dict[str, Count]
    fields: dict[str, dict[str, Count]]
    context_count_shapes: dict[str, Count]

    @model_validator(mode="after")
    def fixed_histograms(self) -> Self:
        if set(self.shapes) != set(SHAPES) or sum(self.shapes.values()) != self.records:
            raise ValueError("invalid_shape_counts")
        if set(self.fields) != set(FIELDS):
            raise ValueError("invalid_counter_fields")
        for histogram in [*self.fields.values(), self.context_count_shapes]:
            if set(histogram) != set(COUNTER_SHAPES) or sum(histogram.values()) != self.records:
                raise ValueError("invalid_counter_counts")
        for histogram in self.fields.values():
            if histogram["missing"] < self.records - self.shapes["object"]:
                raise ValueError("invalid_counter_scope")
        return self


class ChatFixtureReport(_Strict):
    format_version: Annotated[StrictInt, Field(ge=1, le=1)]
    outcome: Literal[
        "observed",
        "busy",
        "source_unavailable",
        "source_ineligible",
        "unsupported_source",
        "source_changed",
        "expired",
        "identity_mismatch",
        "identity_unavailable",
        "organization_mismatch",
        "target_mismatch",
        "rejected",
        "rate_limited",
        "unavailable",
        "timeout",
        "response_limit",
        "unsupported_contract",
    ]
    stage: Literal["source", "account", "organizations", "conversation", "complete"]
    requests: Annotated[StrictInt, Field(ge=0, le=3)]
    http_status: Annotated[StrictInt, Field(ge=100, le=599)] | None
    account_verified: StrictBool
    organization_verified: StrictBool
    target_verified: StrictBool
    history_complete: Literal[False]
    messages: Count
    roles: dict[str, Count]
    identities: dict[str, Count]
    usage: dict[str, ChatUsageScope]
    exact_token_accounting_verified: Literal[False]
    provenance_verified: Literal[False]
    cloud_contract_verified: Literal[False]
    ready_for_ingestion: Literal[False]

    @field_validator(
        "history_complete",
        "exact_token_accounting_verified",
        "provenance_verified",
        "cloud_contract_verified",
        "ready_for_ingestion",
        mode="before",
    )
    @classmethod
    def only_literal_false(cls, value: Any) -> bool:
        if value is not False:
            raise ValueError("unsupported_claim")
        return False

    @model_validator(mode="after")
    def evidence_chain(self) -> Self:
        if (
            set(self.roles) != {"human", "assistant", "unknown"}
            or sum(self.roles.values()) != self.messages
        ):
            raise ValueError("invalid_role_counts")
        if set(self.identities) != {"present", "missing", "invalid", "duplicate"}:
            raise ValueError("invalid_identity_counts")
        if (
            sum(self.identities[name] for name in ("present", "missing", "invalid"))
            != self.messages
        ):
            raise ValueError("invalid_identity_counts")
        if self.identities["duplicate"] > self.identities["present"]:
            raise ValueError("invalid_duplicate_count")
        if set(self.usage) != {"conversation", "messages"}:
            raise ValueError("invalid_usage_locations")
        if self.usage["messages"].records != self.messages:
            raise ValueError("invalid_message_scope")
        if self.requests == 0 and self.http_status is not None:
            raise ValueError("invalid_http_evidence")
        if self.account_verified and self.requests < 1:
            raise ValueError("invalid_account_evidence")
        if self.organization_verified and (not self.account_verified or self.requests < 2):
            raise ValueError("invalid_organization_evidence")
        if self.outcome == "observed":
            if not (
                self.stage == "complete"
                and self.requests == 3
                and self.http_status == 200
                and self.account_verified
                and self.organization_verified
                and self.target_verified
                and self.usage["conversation"].records == 1
            ):
                raise ValueError("invalid_target_evidence")
        elif self.target_verified or self.messages or self.usage["conversation"].records:
            raise ValueError("unverified_observations")
        elif self.stage == "complete":
            raise ValueError("invalid_stage")
        if self.outcome in ("source_changed", "source_unavailable", "timeout") and (
            self.account_verified or self.organization_verified or self.stage != "source"
        ):
            raise ValueError("uncertain_authority")
        return self
