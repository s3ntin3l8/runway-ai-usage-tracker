from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlmodel import SQLModel, Field, create_engine, Session, select
import json


from app.core.encryption import encryption_service


class UsageSnapshot(SQLModel, table=True):
    __tablename__ = "usage_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    provider_id: str = Field(index=True)
    account_id: str = Field(index=True)
    account_label: Optional[str] = None
    service_name: str
    used_value: Optional[float] = None
    limit_value: Optional[float] = None
    unit_type: str = Field(default="generic")
    currency: Optional[str] = None
    tier: Optional[str] = None
    model_id: Optional[str] = None
    window_type: str = Field(default="unknown")
    health: str
    sidecar_id: Optional[str] = Field(default=None, index=True)
    is_unlimited: bool = Field(default=False)
    data_source: str
    error_type: Optional[str] = None
    
    # Store provider-specific data as (possibly encrypted) JSON string
    raw_metadata_json: Optional[str] = Field(default=None)

    @property
    def raw_metadata(self) -> Dict[str, Any]:
        """Decrypt and deserialize metadata."""
        if not self.raw_metadata_json:
            return {}
        return encryption_service.decrypt_json(self.raw_metadata_json)

    @raw_metadata.setter
    def raw_metadata(self, value: Dict[str, Any]):
        """Encrypt and serialize metadata."""
        self.raw_metadata_json = encryption_service.encrypt_json(value)


class SidecarRegistry(SQLModel, table=True):
    """Persistent registry of known sidecars that have sent data."""
    __tablename__ = "sidecar_registry"

    sidecar_id: str = Field(primary_key=True)
    hostname: Optional[str] = None          # socket.gethostname() from sidecar
    custom_name: Optional[str] = None       # User-assigned display name
    tags_json: Optional[str] = Field(default=None)  # JSON array stored as string
    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )
    first_seen: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_ip: Optional[str] = None
    error_count: int = Field(default=0)
    ingest_count: int = Field(default=0)

    @property
    def tags(self) -> List[str]:
        return json.loads(self.tags_json) if self.tags_json else []

    @tags.setter
    def tags(self, value: List[str]):
        self.tags_json = json.dumps(value)
