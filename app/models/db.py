"""Backward-compatibility shim: table classes live in domain modules.

Import this module, not a domain module, before ``SQLModel.metadata.create_all``.
A domain module registers only its own tables.
"""

from app.core.encryption import encryption_service
from app.models.db_claude_cloud import (
    ClaudeCloudCollectorState,
    ClaudeCloudSessionCheckpoint,
    ClaudeCloudSessionUsage,
    ClaudeCloudVerification,
    ClaudeCloudVerificationArchive,
)
from app.models.db_credentials import (
    CredentialSource,
    CredentialTag,
    PendingCredentialTag,
    ProviderAccountLabel,
    ProviderAccountMerge,
    ProviderConfig,
)
from app.models.db_sidecar import (
    SidecarDevice,
    SidecarPairingCode,
    SidecarRegistry,
    SidecarRequestReceipt,
    SidecarSourceCommand,
    SidecarSourceContribution,
    SidecarSourceRevision,
    SidecarSourceState,
)
from app.models.db_system import (
    AuditLog,
    ProviderPricing,
    SessionName,
    SystemConfig,
    WebhookConfig,
    WebhookCredentialAlert,
)
from app.models.db_usage import (
    LatestUsage,
    LatestUsageContribution,
    PendingUsageEvent,
    QuotaSnapshot,
    UsageEvent,
    UsagePeriodRollup,
    UsageWindow,
)

__all__ = [
    "AuditLog",
    "ClaudeCloudCollectorState",
    "ClaudeCloudSessionCheckpoint",
    "ClaudeCloudSessionUsage",
    "ClaudeCloudVerification",
    "ClaudeCloudVerificationArchive",
    "CredentialSource",
    "CredentialTag",
    "encryption_service",
    "LatestUsage",
    "LatestUsageContribution",
    "PendingCredentialTag",
    "PendingUsageEvent",
    "ProviderAccountLabel",
    "ProviderAccountMerge",
    "ProviderConfig",
    "ProviderPricing",
    "QuotaSnapshot",
    "SessionName",
    "SidecarDevice",
    "SidecarPairingCode",
    "SidecarRegistry",
    "SidecarRequestReceipt",
    "SidecarSourceCommand",
    "SidecarSourceContribution",
    "SidecarSourceRevision",
    "SidecarSourceState",
    "SystemConfig",
    "UsageEvent",
    "UsagePeriodRollup",
    "UsageWindow",
    "WebhookConfig",
    "WebhookCredentialAlert",
]
