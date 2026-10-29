"""Builders for Claude Code cloud-runner history records (real shape, invented values).

The shapes come from `docs/evidence/acceptance/claude-cloud-source-shape-20261029.md`: an envelope
with `sequence_num` (a string), `created_at` and a `payload` holding the SDK record. Nothing here is a
real id, path or token count.
"""

from __future__ import annotations

from typing import Any

T0 = "2026-09-24T10:00:00.000000Z"


def _stamp(seconds: int) -> str:
    return f"2026-09-24T10:{seconds // 60:02d}:{seconds % 60:02d}.000000Z"


def assistant(  # noqa: PLR0913 - a record builder takes the fields it sets
    seq: int,
    message_id: str,
    model: str = "claude-opus-5-5",
    *,
    inp: int = 0,
    out: int = 0,
    read: int = 0,
    write_1h: int = 0,
    write_5m: int = 0,
    at: int | None = None,
    stamped: bool = True,
    parent: str | None = None,
    subagent: str | None = None,
    tools: tuple[str, ...] = (),
    stop: str | None = None,
    split: bool = True,
    **usage_extra: Any,
) -> dict[str, Any]:
    seconds = seq if at is None else at
    usage: dict[str, Any] = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write_1h + write_5m,
        "service_tier": "standard",
        **usage_extra,
    }
    if split:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": write_1h,
            "ephemeral_5m_input_tokens": write_5m,
        }
    payload: dict[str, Any] = {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "message": {
            "id": message_id,
            "model": model,
            "stop_reason": stop,
            "usage": usage,
            "content": [
                {"type": "tool_use", "id": f"tool-{index}", "name": name, "input": {}}
                for index, name in enumerate(tools)
            ]
            or [{"type": "text", "text": "x"}],
        },
    }
    if subagent is not None:
        payload["subagent_type"] = subagent
    if stamped:
        payload["timestamp"] = _stamp(seconds)
    return {
        "sequence_num": str(seq),
        "created_at": _stamp(seconds + 0),
        "event_type": "assistant",
        "source": "worker",
        "payload": payload,
    }


def result(
    seq: int, model_usage: dict[str, dict[str, int]], *, at: int | None = None
) -> dict[str, Any]:
    seconds = seq if at is None else at
    return {
        "sequence_num": str(seq),
        "created_at": _stamp(seconds),
        "event_type": "result",
        "source": "worker",
        "payload": {
            "type": "result",
            "subtype": "success",
            "timestamp": _stamp(seconds),
            "modelUsage": {
                model: {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "thinkingTokens": 0,
                    "costUSD": 0.5,
                    **values,
                }
                for model, values in model_usage.items()
            },
        },
    }


def system(seq: int, **fields: Any) -> dict[str, Any]:
    subtype = fields.pop("subtype", "init")
    return {
        "sequence_num": str(seq),
        "created_at": _stamp(seq),
        "event_type": "system",
        "source": "worker",
        "payload": {"type": "system", "subtype": subtype, **fields},
    }
