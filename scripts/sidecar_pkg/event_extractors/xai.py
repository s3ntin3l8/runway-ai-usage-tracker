"""Extract completed Grok CLI turn usage from ``updates.jsonl`` files."""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402

_MAX_COUNTER = 2**63 - 1


def _nonnegative_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _count(data: dict[str, Any], *keys: str) -> int:
    """Return the first valid, non-negative integer field from ``data``."""
    for key in keys:
        value = data.get(key)
        if _nonnegative_number(value) and value <= _MAX_COUNTER:
            return int(value)
    return 0


def _timestamp(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            return fallback
    if isinstance(value, int) and not isinstance(value, bool):
        # ACP journals use RFC3339 timestamps; accept epoch milliseconds too.
        try:
            epoch = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    elif isinstance(value, float) and math.isfinite(value):
        try:
            epoch = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return fallback


def _usage_row(
    raw: Any,
    *,
    model: str,
    usage_incomplete: bool,
    session_id: str,
    prompt_id: str,
    account_id: str,
    ts: datetime,
    cwd: str | None,
    elapsed_ms: int | None,
) -> UsageEventPush | None:
    if not isinstance(raw, dict):
        return None

    input_full = _count(raw, "inputTokens", "input_tokens")
    headless_input = any(
        key in raw
        for key in (
            "cacheReadInputTokens",
            "cache_read_input_tokens",
            "cacheCreationInputTokens",
            "cache_creation_input_tokens",
        )
    )
    cache_read = _count(
        raw,
        "cachedReadTokens",
        "cached_read_tokens",
        "cacheReadInputTokens",
        "cache_read_input_tokens",
    )
    cache_create = _count(
        raw,
        "cacheCreationTokens",
        "cache_creation_tokens",
        "cacheCreationInputTokens",
        "cache_creation_input_tokens",
    )
    output_full = _count(raw, "outputTokens", "output_tokens")
    if not headless_input:
        cache_read = min(cache_read, input_full)
        cache_create = min(cache_create, input_full - cache_read)
    reasoning = min(_count(raw, "reasoningTokens", "reasoning_tokens"), output_full)
    # Cache and reasoning counters are subsets of input/output. Store disjoint
    # buckets because Runway's rollups add each bucket independently.
    input_tokens = input_full if headless_input else max(input_full - cache_read - cache_create, 0)
    output_tokens = max(output_full - reasoning, 0)

    ticks = raw.get("costUsdTicks")
    if ticks is None:
        ticks = raw.get("cost_usd_ticks")
    if ticks is None:
        ticks = raw.get("total_cost_usd_ticks")
    cost_partial = bool(raw.get("costIsPartial", raw.get("cost_is_partial", False)))
    cost_usd = None
    if (
        not usage_incomplete
        and not cost_partial
        and _nonnegative_number(ticks)
        and ticks <= _MAX_COUNTER
    ):
        cost_usd = float(ticks) / 10_000_000_000
    # Headless projection uses a complete floating-point per-model cost.
    if cost_usd is None and not usage_incomplete and not cost_partial:
        candidate = raw.get("costUSD")
        if candidate is None:
            candidate = raw.get("cost_usd")
        if candidate is None:
            candidate = raw.get("total_cost_usd")
        if _nonnegative_number(candidate):
            cost_usd = float(candidate)

    if (
        not any((input_tokens, cache_read, cache_create, output_tokens, reasoning))
        and cost_usd is None
    ):
        return None
    return UsageEventPush(
        provider_id="xai",
        account_id=account_id,
        event_id=f"xai|grok|{session_id}|{prompt_id}|{model}",
        ts=ts.isoformat(),
        model_id=model or "unknown",
        session_id=session_id,
        cwd=cwd,
        latency_ms=elapsed_ms,
        tokens_input=input_tokens,
        tokens_output=output_tokens,
        tokens_cache_read=cache_read,
        tokens_cache_create=cache_create,
        tokens_reasoning=reasoning,
        tool_calls=0,
        effort=None,
        cost_usd=cost_usd,
    )


def parse_xai_events(
    paths: list[Path],
    *,
    account_id: str,
    since: datetime,
    canonical_hints: dict[str, dict[str, str]] | None = None,  # noqa: ARG001
) -> list[UsageEventPush]:
    """Parse turn-completed updates, retaining a one-second watermark overlap.

    Grok update envelopes carry a timestamp, session ID, and ACP update. Some
    older logs store the SessionNotification directly; those use file mtime as
    a timestamp fallback. Stable session/prompt/model IDs make overlap replays
    safe for server-side deduplication.
    """
    events: list[UsageEventPush] = []
    cutoff = since - timedelta(seconds=1)
    for path in paths:
        try:
            fallback_ts = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            encoded_cwd = path.parent.parent.name
            cwd = unquote(encoded_cwd) if encoded_cwd else None
            path_session_id = path.parent.name
            with path.open(encoding="utf-8") as stream:
                # Tool-result updates can be very large. Only turn/model
                # notifications need JSON decoding for usage extraction.
                lines = [line for line in stream if '"sessionUpdate"' in line]
        except (OSError, ValueError):
            continue

        current_model = "unknown"
        for line in lines:
            try:
                envelope = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(envelope, dict):
                continue
            params = envelope.get("params")
            if isinstance(params, dict):
                update = params.get("update")
                session_id = str(params.get("sessionId") or path_session_id)
            else:
                update = envelope.get("update")
                session_id = str(envelope.get("sessionId") or path_session_id)
            if not isinstance(update, dict):
                continue

            kind = update.get("sessionUpdate")
            if kind == "model_changed":
                model_id = update.get("model_id") or update.get("modelId")
                if isinstance(model_id, str) and model_id.strip():
                    current_model = model_id.strip()
                continue
            if kind != "turn_completed":
                continue

            prompt_id = update.get("prompt_id") or update.get("promptId")
            usage = update.get("usage")
            if not isinstance(prompt_id, str) or not prompt_id or not isinstance(usage, dict):
                continue
            ts = _timestamp(envelope.get("timestamp"), fallback_ts)
            if ts <= cutoff:
                continue
            incomplete = bool(
                usage.get("usageIsIncomplete", usage.get("usage_is_incomplete", False))
            )
            elapsed = update.get("elapsed_ms", update.get("elapsedMs"))
            elapsed_ms = (
                int(elapsed) if _nonnegative_number(elapsed) and elapsed <= _MAX_COUNTER else None
            )
            model_usage = usage.get("modelUsage", usage.get("model_usage"))
            turn_events: list[UsageEventPush] = []
            if isinstance(model_usage, dict) and model_usage:
                for model, row in model_usage.items():
                    if isinstance(model, str) and model.strip():
                        event = _usage_row(
                            row,
                            model=model.strip(),
                            usage_incomplete=incomplete,
                            session_id=session_id,
                            prompt_id=prompt_id,
                            account_id=account_id,
                            ts=ts,
                            cwd=cwd,
                            elapsed_ms=elapsed_ms,
                        )
                        if event is not None:
                            turn_events.append(event)
            if not turn_events:
                event = _usage_row(
                    usage,
                    model=current_model,
                    usage_incomplete=incomplete,
                    session_id=session_id,
                    prompt_id=prompt_id,
                    account_id=account_id,
                    ts=ts,
                    cwd=cwd,
                    elapsed_ms=elapsed_ms,
                )
                if event is not None:
                    turn_events.append(event)
            events.extend(turn_events)
    return events
