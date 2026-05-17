import hashlib
import re


def resolve_account_id(
    provider_id: str,  # reserved for future provider-specific rules
    raw_account_id: str | None,
    account_label: str | None,
    credential_hint: str | None = None,
) -> str:
    """Canonical account_id used by both LatestUsage and CumulativeUsage."""
    email_pattern = r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$"

    # Pre-process "email @ org" format (e.g. "user@company.com @ MyOrg")
    label = account_label
    if label and " @ " in label:
        label = label.split(" @ ")[0].strip()

    if label and re.match(email_pattern, label):
        return label.lower()

    if raw_account_id and raw_account_id != "default" and re.match(email_pattern, raw_account_id):
        return raw_account_id.lower()

    if raw_account_id and raw_account_id != "default":
        return raw_account_id

    if credential_hint:
        # Full SHA-256 — previously truncated to 12 chars (48 bits), which the
        # audit flagged for birthday-paradox collision risk at scale. The full
        # digest has no functional downside (account_id is opaque to consumers)
        # and removes the theoretical collision floor.
        return hashlib.sha256(credential_hint.encode()).hexdigest()

    return "default"
