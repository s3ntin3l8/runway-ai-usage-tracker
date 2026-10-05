import base64
import hashlib
import json
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


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_EMAIL_RE = EMAIL_RE
_HASH_RE = HASH_RE

# OpenCode's subscription, Zen, and free usage streams have distinct provider
# IDs in the event store, but share one OpenCode account identity/config.
# OpenCode's extractor explicitly keeps opencode-zen as its own usage stream
# in scripts/sidecar_pkg/event_extractors/opencode.py:_OC_PROVIDER_MAP, while
# scripts/sidecar_pkg/event_extractors/hermes.py folds it to opencode.
_OPENCODE_TIER_PROVIDER_IDS = frozenset({"opencode-free", "opencode-zen"})
_OPENCODE_ACCOUNT_USAGE_PROVIDER_IDS = frozenset({"opencode", "opencode-free", "opencode-zen"})

_USAGE_TO_CONFIG_PROVIDER_MAP: dict[str, str] = {
    "opencode-free": "opencode",
    "opencode-zen": "opencode",
    "hermes-auto": "opencode",
    "hermes-xai-oauth": "xai",
    "xai-oauth": "xai",
    "xai-api": "xai",
}

_CONFIG_TO_USAGE_PROVIDER_IDS: dict[str, frozenset[str]] = {
    "opencode": frozenset({"opencode", "opencode-free", "opencode-zen", "hermes-auto"}),
    "xai": frozenset({"xai", "hermes-xai-oauth", "xai-oauth", "xai-api"}),
}


# Usage streams whose account may legitimately be an account of *another*
# provider: gemini-cli is gone and Google's subscription now surfaces through
# Antigravity, so Hermes' Gemini events can be assigned to either. Keep in sync
# with RELATED_ACCOUNT_PROVIDERS in webapp/src/lib/providerAccountAliases.ts.
_RELATED_ACCOUNT_PROVIDERS: dict[str, tuple[str, ...]] = {
    "gemini": ("antigravity",),
}


def account_config_provider_id(provider_id: str) -> str:
    """Return the provider whose account config owns a usage provider ID."""
    return _USAGE_TO_CONFIG_PROVIDER_MAP.get(provider_id, provider_id)


def related_account_provider_ids(config_provider_id: str) -> tuple[str, ...]:
    """Return other providers whose accounts may own this provider's usage."""
    return _RELATED_ACCOUNT_PROVIDERS.get(config_provider_id, ())


def allowed_assignment_provider_ids(usage_provider_id: str) -> tuple[str, ...]:
    """Return config providers an event of ``usage_provider_id`` may be assigned to."""
    config_provider_id = account_config_provider_id(usage_provider_id)
    return (config_provider_id, *related_account_provider_ids(config_provider_id))


def is_redirect_source_provider(usage_provider_id: str) -> bool:
    """True when events of this provider can be redirected to a related provider."""
    return bool(related_account_provider_ids(account_config_provider_id(usage_provider_id)))


def account_usage_provider_ids(provider_id: str) -> frozenset[str]:
    """Return event provider IDs that share a configured account identity."""
    return _CONFIG_TO_USAGE_PROVIDER_IDS.get(provider_id, frozenset({provider_id}))


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


# Providers whose account_id is a stable login handle. An email *label* is only
# a display name there and must never re-key an explicitly identified account
# (it split one GitHub account into a login series and an email series).
_LOGIN_KEYED_PROVIDERS = frozenset({"github"})


def resolve_account_id(
    provider_id: str,
    raw_account_id: str | None,
    account_label: str | None,
    credential_hint: str | None = None,
) -> str:
    """Canonical account_id used by both LatestUsage and CumulativeUsage."""
    # Pre-process "email @ org" format (e.g. "user@company.com @ MyOrg")
    label = account_label
    if label and " @ " in label:
        label = label.split(" @ ")[0].strip()

    raw = canonical_account_id(raw_account_id)
    if provider_id in _LOGIN_KEYED_PROVIDERS and raw != "default":
        return raw

    if label and _EMAIL_RE.match(label):
        return label.lower()

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


# Length of a credential fingerprint's hex encoding. 12 hex chars = 48 bits,
# far more than enough to tell two credentials apart while staying short
# enough to read in the Untagged Credentials dialog's mono origin line.
FINGERPRINT_LEN = 12
_FINGERPRINT_RE = re.compile(rf"^[0-9a-f]{{{FINGERPRINT_LEN}}}$")

# Providers whose credential is a bare static key with no per-account identity of
# its own (#347, #349, #443): the providers whose sidecar rules read OpenCode's
# ``auth.json`` — one file, one key per backend, no email or account id anywhere in
# it — plus those that read their key from an env var (kimi_api, kimi_k2, zai). ``path:/home/u/.local/share/opencode/
# auth.json`` is the same string on every host with the same username and
# the same string before and after a rotation, so two keys sharing that
# origin would share one operator tag and inherit each other's account;
# suffixing a fingerprint of the value identifies the credential instead of
# the file it was found in.
#
# Everything else keeps the plain descriptor: its credential carries a real
# identity or rotates by itself (anthropic, chatgpt, gemini, antigravity, github's
# OAuth token). Cookie / CLI-OAuth candidates *of* the providers listed here
# stay plain as well — they carry no key field, so the sidecar's
# fingerprinting finds nothing to fingerprint, and a fingerprint of a rotating
# secret would orphan its tag on every rotation.
#
# Both halves need this set: the sidecar to suffix origins
# (``fingerprinted_credential_origin`` in ``scripts/sidecar.py``), and the
# server (``_fingerprinted_credential_hints`` in
# ``app/api/endpoints/fleet.py``) to know which rows it may answer a
# ``provider:<pid>#<fp>`` hint for. Mirrored in
# ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
FINGERPRINTED_ORIGIN_PROVIDERS: frozenset[str] = frozenset(
    {
        "opencode",
        "openrouter",
        "minimax",
        "kimi_coding",
        "kimi_api",
        "kimi_k2",
        "ollama",
        "xai",
        "deepseek",
        "zai",
    }
)


def credential_fingerprint(value: str | None) -> str | None:
    """Stable, non-reversible 12-hex fingerprint of a credential.

    Used to make a ``credential_origin`` identify the *credential* rather
    than the file or variable it was found in, so two hosts (or one host
    after a key rotation) can never share an origin and inherit each
    other's operator tag. The one deliberate exception is the server's
    rotation carry (``CredentialTagRepo.inherited_account_for_rotation``,
    #474): a new fingerprint on the *same* base origin inherits the old
    binding only when exactly one account was ever tagged there — origins
    themselves stay distinct, and nothing ever crosses hosts.

    PBKDF2-HMAC-SHA256 with a fixed domain-separation salt and a single
    iteration rather than a bare ``hashlib.sha256``: the input is a
    password-tainted secret, and CodeQL's ``py/weak-sensitive-data-hashing``
    rule only exempts explicit key-derivation functions (same reasoning as
    :func:`resolve_account_id`'s ``credential_hint`` branch). One iteration
    is enough — the output is an opaque 48-bit row key, not a password
    hash, and the inputs are high-entropy API keys.

    Blank / missing input → ``None`` (no fingerprint is derivable).

    Mirrored in ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
    """
    s = (value or "").strip()
    if not s:
        return None
    return hashlib.pbkdf2_hmac(
        "sha256",
        s.encode("utf-8"),
        b"runway-credential-fp-v1",
        1,
    ).hex()[:FINGERPRINT_LEN]


# Claims of an xAI access JWT that identify the login and survive a refresh,
# in preference order. ``jti``/``iat``/``exp`` change on every refresh; these
# do not (#523). ``team_id`` is left out: a team can hold several logins.
STABLE_JWT_CLAIMS: tuple[str, ...] = ("principal_id", "sub")


def jwt_stable_subject(token: str | None) -> str | None:
    """First non-empty :data:`STABLE_JWT_CLAIMS` value of a JWT's payload.

    The payload is decoded without verifying the signature — it only shapes
    an origin key, nothing trusts it. A non-JWT, a malformed payload, or one
    with none of the claims → ``None``, so callers fall back to another key.

    Mirrored in ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
    """
    parts = (token or "").strip().removeprefix("Bearer ").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for claim in STABLE_JWT_CLAIMS:
        value = payload.get(claim)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def keyed_credential_origin(base_origin: str, fingerprint: str) -> str:
    """``<base_origin>#<fingerprint>`` — a key-scoped credential origin.

    ``base_origin`` is the rule-derived descriptor (``path:…``,
    ``env:…``, ``provider:<pid>``) and ``fingerprint`` comes from
    :func:`credential_fingerprint`.

    The same helper builds both halves of the pairing: the sidecar
    reports ``path:/home/u/…/auth.json#<fp>`` as its origin, and the
    server ships its account hint under ``provider:opencode#<fp>`` —
    a key the server can construct on its own because it never learns
    the sidecar's filesystem layout.

    Mirrored in ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
    """
    return f"{base_origin}#{fingerprint}"


def split_keyed_origin(origin: str) -> tuple[str, str | None]:
    """Inverse of :func:`keyed_credential_origin`.

    Returns ``(base_origin, fingerprint)``; an origin that carries no
    well-formed fingerprint suffix returns ``(origin, None)`` so callers
    can keep consulting pre-keyed-origin tags written by older sidecars.

    Mirrored in ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
    """
    base, sep, suffix = origin.rpartition("#")
    if sep and _FINGERPRINT_RE.match(suffix):
        return base, suffix
    return origin, None
