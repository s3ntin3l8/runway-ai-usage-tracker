"""Backward-compatible re-exports. Implementation: app.services.credentials.tags and pending_tags."""

from app.services.credentials.pending_tags import (
    VERIFY_BACKOFF_BASE,
    VERIFY_BACKOFF_MAX,
    PendingCredentialTagRepo,
    retry_backoff,
)
from app.services.credentials.tags import (
    QUOTA_PREVIEW_FIELDS,
    CredentialTagRepo,
    ParkedMapping,
    archived_account_pairs,
    archived_account_pairs_from_configs,
    live_sidecar_ids,
    origin_candidates,
    pick_effective_tag,
    safe_quota_preview,
)

__all__ = [
    "QUOTA_PREVIEW_FIELDS",
    "safe_quota_preview",
    "live_sidecar_ids",
    "origin_candidates",
    "pick_effective_tag",
    "archived_account_pairs_from_configs",
    "archived_account_pairs",
    "ParkedMapping",
    "CredentialTagRepo",
    "VERIFY_BACKOFF_BASE",
    "VERIFY_BACKOFF_MAX",
    "retry_backoff",
    "PendingCredentialTagRepo",
]
