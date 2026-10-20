"""Model-id normalizers shared by the sidecar extractors and server ingest.

Live in ``app/`` (the server image ships only ``app/``) so the event ingestor
can re-normalize a redirected event's raw model id for its target provider.
"""

import re


def is_openrouter_model_id(model_id: str | None) -> bool:
    """True for a model id only OpenRouter can have served.

    ``openrouter/<...>`` (its router aliases) and ``<vendor>/<model>:free`` (its
    free-tier naming). A bare local id such as ``qwen/qwen3-coder-30b`` (LM Studio,
    Ollama) is vendor-prefixed but not an OpenRouter signal, so it does not count.
    The SQL twin is ``maintenance.openrouter_retag.model_clause`` — keep them in sync.
    """
    lower = (model_id or "").strip().lower()
    if lower.startswith("openrouter/"):
        return True
    return "/" in lower and lower.endswith(":free")


def is_free_tier_model_id(model_id: str | None) -> bool:
    """True for a model id that names a free-tier route.

    A free route is billed at $0 — it is not a paid model that happens to be
    missing a rate. The suffix forms are the ones the sidecar's
    ``_is_free_model`` recognises: a trailing ``":free"``, ``"-free"`` or
    ``"/free"``, or a bare ``"free"`` id. That covers OpenCode's ``-free``
    catalog entries (``deepseek-v4-flash-free``) and OpenRouter's ``:free``
    entries (``vendor/model:free``). The ``openrouter/free/*`` prefix form is
    included as well.

    This is the *pricing* notion of "free tier", and it is deliberately
    broader than the sidecar's ``_is_free_model`` — that one decides which
    provider bucket a session files under, and widening it would re-route
    ``openrouter/free/*`` sessions between providers. See
    ``scripts/sidecar_pkg/event_extractors/hermes.py``.

    The cost calculator uses this to keep a free id from reaching a paid
    sibling row through its version-strip or segment-trim fallbacks, and the
    Data Health ``unpriced_models`` check uses it to leave free ids out of
    its report.
    """
    m = (model_id or "").strip().lower()
    if not m:
        return False
    if m == "openrouter/free" or m.startswith("openrouter/free/"):
        return True
    if m.endswith((":free", "-free", "/free")):
        return True
    last_token = m.replace(":", "/").split("/")[-1]
    # Only the bare "free" id reaches here. A last token that ends in
    # "-free" implies the whole id does, so the suffix check above already
    # returned for it; the sidecar's copy of this predicate keeps the
    # equivalent (dead) clause, and this one does not.
    return last_token == "free"


# ── Gemini 4 Argon ──

# Google prices Gemini 4 Argon as one family (issue #686). The seeded pricing
# rows are keyed on the canonical id for both the `gemini` and the
# `antigravity` provider — see pricing_seed.py.
ARGON_MODEL_ID = "gemini-4-argon"

# The Antigravity CLI reports the raw id as "gemini-4-argon-trial": the
# "-trial" suffix is part of the id, not a version qualifier, so it must not
# survive into the stored model_id. Google publishes the model as
# "gemini-4-argon" (https://ai.google.dev/gemini-api/docs/models/gemini-4-argon).
_ARGON_RAW_RE = re.compile(r"^gemini-4-argon(?:[-.][a-z0-9][-a-z0-9.]*)?$")

# The display name carries the model name in words ("Gemini 4 Argon (High)"),
# so the match needs word boundaries: a look-alike such as "Argonoid" is a
# different model, not a spelling of Argon.
_ARGON_DISPLAY_RE = re.compile(r"\bargon\b", re.IGNORECASE)


def is_argon_model_id(model_id: str) -> bool:
    """True for any spelling of the Gemini 4 Argon id."""
    return bool(_ARGON_RAW_RE.match((model_id or "").strip().lower()))


def normalize_gemini_model(model_name: str) -> str:
    """Map raw Gemini model strings to versioned cost buckets.

    Each tier × major-version pair gets its own bucket because Google charges
    distinct rates per https://ai.google.dev/gemini-api/docs/pricing. Pricing
    rows for these ids live in app/services/pricing_seed.py.

    Quota cards (in app/services/collectors/gemini_api.py) keep the coarser
    pro/flash/flash-lite buckets since the families share Google's quota.

    Gemini 4 Argon is priced as one family under its own canonical id, so it
    bypasses the tier buckets entirely (issue #686).

    Examples:
        "gemini-2.5-flash"          → "flash-2.5"
        "gemini-2.5-flash-lite"     → "flash-lite-2.5"
        "gemini-2.5-pro"            → "pro-2.5"
        "gemini-3-flash-preview"    → "flash-3-preview"
        "gemini-3.1-flash"          → "flash-3.1"
        "gemini-3.1-flash-lite"     → "flash-lite-3.1"
        "gemini-3-pro-preview"      → "pro-3.1-preview"
        "gemini-3.1-pro-preview"    → "pro-3.1-preview"
        "gemini-4-argon-trial"      → "gemini-4-argon"
        ""                           → "unknown"
    """
    lower = (model_name or "").lower()
    if not lower:
        return "unknown"
    if is_argon_model_id(lower):
        return ARGON_MODEL_ID
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


def _ag_argon_bucket(raw: str, display: str) -> str | None:
    """Return the canonical Argon id when raw/display name Gemini 4 Argon.

    The raw id is authoritative — a ``gemini-4-argon*`` slug is Argon whatever
    the display says. A display-only match is scoped exactly like the family
    fallback in ``normalize_ag_model``: it applies only when the raw id names
    no family of its own, so a raw id that does (``gemini-3.8-flash``) keeps
    its own bucket and a non-Gemini raw id (``gpt-oss``) passes through
    verbatim even when the display happens to name Argon.
    """
    if is_argon_model_id(raw):
        return ARGON_MODEL_ID
    if raw and (_ag_detect_family(raw) is not None or not raw.lower().startswith("gemini")):
        return None
    lower_display = display.lower()
    if "gemini" in lower_display and _ARGON_DISPLAY_RE.search(display):
        return ARGON_MODEL_ID
    return None


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

    Gemini 4 Argon is priced as one family under its own canonical id, so it
    bypasses the tier buckets entirely: the raw id carries a "-trial" suffix
    (``gemini-4-argon-trial``) that must not reach the stored model_id
    (issue #686).

    Examples:
        "claude-sonnet-4-6" / ""                 → "claude-sonnet"
        "claude-opus-4-6" / ""                   → "claude-opus"
        "gemini-3.5-flash" / "Gemini 3.5 Flash" → "flash-3.5"
        "gemini-pro-default" / "Gemini 3.1 Pro" → "pro-3.1"
        "gemini-default" / "Gemini 3.5 Flash"   → "flash-3.5"
        "gemini-3-flash-a" / "Gemini 3 Flash"   → "flash-3"
        "gemini-3.1-flash" / …                  → "flash-3"  (unseeded minor)
        "gemini-4-argon-trial" / "Gemini 4 Argon (High)" → "gemini-4-argon"
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

    # Gemini 4 Argon (issue #686): the raw id carries a "-trial" suffix and
    # Google publishes the model as "gemini-4-argon" — both spellings must
    # reach the seeded row, which is keyed on the canonical id.
    argon = _ag_argon_bucket(raw, display)
    if argon is not None:
        return argon

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
