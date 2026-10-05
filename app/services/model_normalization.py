"""Model-id normalizers shared by the sidecar extractors and server ingest.

Live in ``app/`` (the server image ships only ``app/``) so the event ingestor
can re-normalize a redirected event's raw model id for its target provider.
"""

import re


def normalize_gemini_model(model_name: str) -> str:
    """Map raw Gemini model strings to versioned cost buckets.

    Each tier × major-version pair gets its own bucket because Google charges
    distinct rates per https://ai.google.dev/gemini-api/docs/pricing. Pricing
    rows for these ids live in app/services/pricing_seed.py.

    Quota cards (in app/services/collectors/gemini_api.py) keep the coarser
    pro/flash/flash-lite buckets since the families share Google's quota.

    Examples:
        "gemini-2.5-flash"          → "flash-2.5"
        "gemini-2.5-flash-lite"     → "flash-lite-2.5"
        "gemini-2.5-pro"            → "pro-2.5"
        "gemini-3-flash-preview"    → "flash-3-preview"
        "gemini-3.1-flash"          → "flash-3.1"
        "gemini-3.1-flash-lite"     → "flash-lite-3.1"
        "gemini-3-pro-preview"      → "pro-3.1-preview"
        "gemini-3.1-pro-preview"    → "pro-3.1-preview"
        ""                           → "unknown"
    """
    lower = (model_name or "").lower()
    if not lower:
        return "unknown"
    is_3x = "gemini-3" in lower
    if "flash-lite" in lower:
        return "flash-lite-3.1" if is_3x else "flash-lite-2.5"
    if "flash" in lower:
        if not is_3x:
            return "flash-2.5"
        return "flash-3-preview" if "preview" in lower else "flash-3.1"
    if "pro" in lower:
        return "pro-3.1-preview" if is_3x else "pro-2.5"
    if "ultra" in lower:
        return "ultra"
    return model_name or "unknown"


# ── Antigravity ──


def _ag_detect_family(s: str) -> str | None:
    """Return flash-lite / flash / pro when ``s`` names a Gemini family."""
    lower = s.lower()
    if "flash" in lower and "lite" in lower:
        return "flash-lite"
    if "flash" in lower:
        return "flash"
    if "pro" in lower:
        return "pro"
    return None


def _ag_extract_version(s: str) -> str | None:
    """Return the first ``major.minor`` token in ``s`` (e.g. ``3.5``)."""
    m = re.search(r"\d+\.\d+", s)
    return m.group(0) if m else None


def _ag_extract_3x(s: str) -> str | None:
    """Return the first Gemini-3 minor (``3.x``) token in ``s``, if any.

    Unlike ``_ag_extract_version``, this skips non-3.x minors (``2.5``,
    ``1.5``) so a stale display name cannot mask a 3.x signal that only
    lives in the raw id (or a later token).
    """
    for m in re.finditer(r"\d+\.\d+", s):
        ver = m.group(0)
        if ver.startswith("3."):
            return ver
    return None


def _ag_looks_like_3x(raw: str, display: str) -> bool:
    """True when there is a Gemini-3 signal but no extracted minor version."""
    if "gemini-3" in raw.lower():
        return True
    # Standalone major "3" in the display ("Gemini 3 Flash"), not "3.5".
    return re.search(r"(?<![\d.])3(?![\d.])", display) is not None


# Minors with dedicated antigravity pricing rows in pricing_seed.py. Any other
# 3.x minor clamps to the major-only bucket: emitting flash-3.1 would miss the
# exact row, version-strip to the cheaper bare "flash" family, and under-bill
# with no warning (only segment-trim logs).
_SEEDED_MINORS: dict[str, frozenset[str]] = {
    "flash": frozenset({"3.5", "3.6", "3.7", "3.8"}),
    "pro": frozenset({"3.1"}),
}


def _ag_versioned_bucket(family: str, version: str) -> str:
    """``flash`` + ``3.7`` → ``flash-3.7``; unseeded minors → ``{family}-3``."""
    if family in _SEEDED_MINORS and version in _SEEDED_MINORS[family]:
        return f"{family}-{version}"
    return f"{family}-3"


def normalize_ag_model(raw_model: str, display_name: str, kv: dict[str, str]) -> str:
    """Map the raw agy model string to a stable cost-bucket id.

    The raw model id (f1.19) is authoritative whenever present: Claude slugs
    (``claude-sonnet-4-6``, ``anthropic-claude-sonnet-4-6``) collapse to their
    family so the cost_calculator matches the seeded Anthropic pricing rows,
    and Gemini slugs fall through to the version/family logic below —
    ``used_claude*`` KV flags are ignored while raw is present (they latch
    per-conversation once Claude is touched and otherwise stamp Gemini-raw
    turns as ``claude-opus``). When raw is empty, the display path runs first;
    flags are only consulted if that path would return ``unknown``.

    Prefer minor-version buckets (``flash-3.5``, ``pro-3.1``, …) when the raw
    id or display name (f1.21, e.g. "Gemini 3.1 Pro (High)") carries a
    Gemini-3.x version — Google charges distinct rates per
    https://ai.google.dev/gemini-api/docs/pricing. Flash-lite stays
    ``flash-lite-3`` / ``flash-lite`` (no versioned lite rows). Non-family raw
    ids (``gpt-oss``, …) pass through verbatim.

    Examples:
        "claude-sonnet-4-6" / ""                 → "claude-sonnet"
        "claude-opus-4-6" / ""                   → "claude-opus"
        "gemini-3.5-flash" / "Gemini 3.5 Flash" → "flash-3.5"
        "gemini-pro-default" / "Gemini 3.1 Pro" → "pro-3.1"
        "gemini-default" / "Gemini 3.5 Flash"   → "flash-3.5"
        "gemini-3-flash-a" / "Gemini 3 Flash"   → "flash-3"
        "gemini-3.1-flash" / …                  → "flash-3"  (unseeded minor)
        "gpt-oss"                               → "gpt-oss"
        ""                                      → "unknown"
    """
    # Call sites default missing field 19 to "" (not "unknown") so empty and
    # decoded-empty raw take the same display-only path.
    raw = (raw_model or "").strip()
    display = (display_name or "").strip()

    # Claude family bucket from the raw slug (raw wins over used_claude*).
    # Keeps family ids only — no claude-sonnet-4.6 versioned pricing rows.
    # Case-insensitive substring: covers CLAUDE-* and aliased slugs
    # (anthropic-claude-sonnet-4-6); must not fall through to Gemini/$0.
    lower_raw = raw.lower()
    if "claude" in lower_raw:
        if "sonnet" in lower_raw:
            return "claude-sonnet"
        # Opus and unseeded tiers (haiku, …) share the seeded claude-opus
        # row (issue #302 accepted policy): better to overbill at Opus than
        # land on $0 via verbatim pass-through with no pricing row.
        return "claude-opus"

    if not raw and not display:
        if kv.get("used_claude_conservative") == "true":
            return "claude-opus"
        if kv.get("used_claude") == "true":
            return "claude-sonnet"
        return "unknown"

    # Empty raw: take family from the display; require a minor version except
    # for a major-only Gemini-3 display (same contract as the raw-present path).
    # Flags are NOT consulted here — only after this path yields unknown
    # (below), so a Gemini display + latched flags still buckets as Gemini.
    if not raw:
        family = _ag_detect_family(display)
        if family is not None:
            version = _ag_extract_version(display)
            if family == "flash-lite":
                if version is not None and version.startswith("3."):
                    return "flash-lite-3"
                if _ag_looks_like_3x(raw, display):
                    return "flash-lite-3"
                if version is not None:
                    return "flash-lite"
            elif version is not None and version.startswith("3."):
                return _ag_versioned_bucket(family, version)
            elif version is None:
                # Family but no minor: "Gemini 3 Flash" → flash-3; "Gemini
                # Flash" stays unknown (base required both family and minor).
                if _ag_looks_like_3x(raw, display):
                    return f"{family}-3"
            else:
                return family

        # Display path yielded unknown (no family, or family without a usable
        # version): last-resort flag fallback (defensive; never observed).
        if kv.get("used_claude_conservative") == "true":
            return "claude-opus"
        if kv.get("used_claude") == "true":
            return "claude-sonnet"
        return "unknown"

    # Prefer family from raw. Fall back to the display only when raw is a
    # Gemini-prefixed id with no family of its own (``gemini-default``) —
    # non-family raw ids (``gpt-oss``) must pass through verbatim even when
    # the display happens to name a Gemini family.
    family = _ag_detect_family(raw)
    if family is None and raw.lower().startswith("gemini"):
        family = _ag_detect_family(display)
    if family is None:
        return raw

    # Prefer a 3.x minor from either field (display first), then any minor
    # (display first). A non-3.x display token must not mask raw's gemini-3.x id.
    version = (
        _ag_extract_3x(display)
        or _ag_extract_3x(raw)
        or _ag_extract_version(display)
        or _ag_extract_version(raw)
    )

    if family == "flash-lite":
        if version is not None and version.startswith("3."):
            return "flash-lite-3"
        if _ag_looks_like_3x(raw, display):
            return "flash-lite-3"
        return "flash-lite"

    if version is not None and version.startswith("3."):
        return _ag_versioned_bucket(family, version)

    # Major-only Gemini-3 in either field (no 3.x minor resolved) → flash-3 / pro-3.
    # A 3.x version already returned above; a non-3.x display minor (2.5 / 1.5)
    # must not mask a major-3 raw id.
    if _ag_looks_like_3x(raw, display):
        return f"{family}-3"

    # Non-3.x minor (e.g. 2.5) or no version at all → bare family bucket.
    return family
