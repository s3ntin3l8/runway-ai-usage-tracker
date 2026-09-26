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


# Length of a credential fingerprint's hex encoding. 12 hex chars = 48 bits,
# far more than enough to tell two credentials apart while staying short
# enough to read in the Untagged Credentials dialog's mono origin line.
FINGERPRINT_LEN = 12
_FINGERPRINT_RE = re.compile(rf"^[0-9a-f]{{{FINGERPRINT_LEN}}}$")

# Providers whose credential is a bare key with no per-account identity of
# its own (#347, #349): exactly the providers whose sidecar rules read
# OpenCode's ``auth.json`` — one file, one key per backend, no email or
# account id anywhere in it. ``path:/home/u/.local/share/opencode/
# auth.json`` is the same string on every host with the same username and
# the same string before and after a rotation, so two keys sharing that
# origin would share one operator tag and inherit each other's account;
# suffixing a fingerprint of the value identifies the credential instead of
# the file it was found in.
#
# Everything else keeps the plain descriptor: its credential carries a real
# identity (anthropic, chatgpt), or key-scoping was scoped out of #349
# (``kimi``). Cookie / CLI-OAuth candidates *of* the providers listed here
# stay plain as well — they carry no key field, so the sidecar's
# fingerprinting finds nothing to fingerprint.
#
# Both halves need this set: the sidecar to suffix origins
# (``fingerprinted_credential_origin`` in ``scripts/sidecar.py``), and the
# server (``_fingerprinted_credential_hints`` in
# ``app/api/endpoints/fleet.py``) to know which rows it may answer a
# ``provider:<pid>#<fp>`` hint for. Mirrored in
# ``scripts/sidecar_pkg/identity.py`` — keep the two in sync.
FINGERPRINTED_ORIGIN_PROVIDERS: frozenset[str] = frozenset(
    {"opencode", "openrouter", "minimax", "kimi_coding", "ollama", "xai"}
)


def credential_fingerprint(value: str | None) -> str | None:
    """Stable, non-reversible 12-hex fingerprint of a credential.

    Used to make a ``credential_origin`` identify the *credential* rather
    than the file or variable it was found in, so two hosts (or one host
    after a key rotation) can never share an origin and inherit each
    other's operator tag.

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
