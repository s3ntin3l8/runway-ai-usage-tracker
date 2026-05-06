import hashlib
import re


def resolve_account_id(
    provider_id: str,
    raw_account_id: str | None,
    account_label: str | None,
    credential_hint: str | None = None,
) -> str:
    """Canonical account_id used by both LatestUsage and CumulativeUsage."""
    email_pattern = r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,6}$"

    if account_label and re.match(email_pattern, account_label):
        return account_label.lower()

    if raw_account_id and raw_account_id != "default" and re.match(email_pattern, raw_account_id):
        return raw_account_id.lower()

    if raw_account_id and raw_account_id != "default":
        return raw_account_id

    if credential_hint:
        return hashlib.sha256(credential_hint.encode()).hexdigest()[:12]

    return "default"
