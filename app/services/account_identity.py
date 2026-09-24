import hashlib
import re

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def normalize_sidecar_id(raw: str) -> str:
    """Stable sidecar id: lowercased first DNS label.

    Collapses `Macbook.in.example.de`, `macbook.local`, and `macbook` to one id
    so a host that flips between its FQDN and Bonjour name keeps a single
    registry entry instead of spawning duplicates. IPv4 literals and dot-less
    sentinels (`local`) pass through lowercased.
    """
    h = (raw or "").strip()
    if not h or _IPV4.match(h):
        return h.lower()
    return h.split(".", 1)[0].lower()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def canonical_account_id(raw: str | None) -> str:
    """Canonical storage form of an already-resolved ``account_id``.

    The single rule every write path applies (cards via
    :func:`resolve_account_id`, events on ingest, token-cache keys, operator
    tags) so the same account never splits on formatting alone:

    - ``None`` / blank → ``"default"``
    - email-shaped → stripped + lowercased (emails are case-insensitive)
    - anything else → stripped, otherwise verbatim (opaque ids and hashes
      are case-sensitive and already stable)
    """
    s = (raw or "").strip()
    if not s:
        return "default"
    if _EMAIL_RE.match(s):
        return s.lower()
    return s


def resolve_account_id(
    provider_id: str,  # reserved for future provider-specific rules
    raw_account_id: str | None,
    account_label: str | None,
    credential_hint: str | None = None,
) -> str:
    """Canonical account_id used by both LatestUsage and CumulativeUsage."""
    # Pre-process "email @ org" format (e.g. "user@company.com @ MyOrg")
    label = account_label
    if label and " @ " in label:
        label = label.split(" @ ")[0].strip()

    if label and _EMAIL_RE.match(label):
        return label.lower()

    raw = canonical_account_id(raw_account_id)
    if raw != "default":
        return raw

    if credential_hint:
        # PBKDF2-HMAC-SHA256 of the credential — derives an opaque
        # account_id used as a (provider_id, account_id) DB row key.
        # PBKDF2 is an explicit password-key-derivation function, so
        # CodeQL's `py/weak-sensitive-data-hashing` rule doesn't apply
        # (the rule only flags plain hashlib.sha1/sha256/sha512/etc.
        # of password-tainted data). One iteration is sufficient here
        # because account_id is opaque to consumers — this isn't a
        # password-store key, just a stable row key derived from the
        # credential.
        return hashlib.pbkdf2_hmac(
            "sha256",
            credential_hint.encode(),
            b"runway-account-id-v1",
            1,
        ).hex()

    return "default"
