import json
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Field, SQLModel

from app.core.encryption import encryption_service


class UsageSnapshot(SQLModel, table=True):
    __tablename__ = "usage_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), index=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(index=True)
    account_label: str | None = None
    service_name: str
    used_value: float | None = None
    limit_value: float | None = None
    unit_type: str = Field(default="generic")
    currency: str | None = None
    tier: str | None = None
    model_id: str | None = None
    window_type: str = Field(default="unknown")
    health: str
    sidecar_id: str | None = Field(default=None, index=True)
    is_unlimited: bool = Field(default=False)
    data_source: str
    error_type: str | None = None
    
    # Store provider-specific data as (possibly encrypted) JSON string
    raw_metadata_json: str | None = Field(default=None)

    @property
    def raw_metadata(self) -> dict[str, Any]:
        """Decrypt and deserialize metadata."""
        if not self.raw_metadata_json:
            return {}
        return encryption_service.decrypt_json(self.raw_metadata_json)

    @raw_metadata.setter
    def raw_metadata(self, value: dict[str, Any]):
        """Encrypt and serialize metadata."""
        self.raw_metadata_json = encryption_service.encrypt_json(value)


class SidecarRegistry(SQLModel, table=True):
    """Persistent registry of known sidecars that have sent data."""
    __tablename__ = "sidecar_registry"

    sidecar_id: str = Field(primary_key=True)
    hostname: str | None = None          # socket.gethostname() from sidecar
    custom_name: str | None = None       # User-assigned display name
    tags_json: str | None = Field(default=None)  # JSON array stored as string
    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(UTC), index=True
    )
    first_seen: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    last_ip: str | None = None
    error_count: int = Field(default=0)
    ingest_count: int = Field(default=0)

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json) if self.tags_json else []

    @tags.setter
    def tags(self, value: list[str]):
        self.tags_json = json.dumps(value)


class WebhookConfig(SQLModel, table=True):
    """Per-provider webhook alert configuration."""
    __tablename__ = "webhook_configs"

    id: int | None = Field(default=None, primary_key=True)
    provider_id: str  # provider name e.g. "anthropic", or "*" for global
    threshold_pct: float  # 0.0–100.0, e.g. 90.0
    url: str  # Discord or Slack incoming webhook URL
    channel: str  # "discord" or "slack" — validated by CRUD API at ingestion
    active: bool = Field(default=True)
    last_fired_at: datetime | None = Field(default=None)  # None = reset/ready to fire
