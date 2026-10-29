"""Usage events from the history of a Claude Code cloud-runner session.

Pure logic, no network and no database: ordered history records go in, ``UsageEventPush`` objects
come out. The rules are specified in ``docs/claude-cloud-event-contract.md``.

A *turn* ends at a ``result`` record. When it closes, one **message event** is emitted per distinct
assistant ``message.id`` (the last record of that id), and one **remainder event** per model holding
what the result's cumulative ``modelUsage`` reports beyond those messages (side calls, the real output,
side models). Totals per turn and model therefore equal the ``modelUsage`` delta exactly.

The reducer keeps only what is needed to continue after the last closed turn (``ReducerState``); the
records of an open turn are re-read by the caller after a restart, and every event id is derived from
the history, so emitting an event twice is harmless (the ingest dedup absorbs it).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.date_utils import parse_iso8601_utc
from app.core.source_apps import CLAUDE_CODE
from app.models.schemas import UsageEventPush
from app.services.model_normalization import normalize_anthropic_model

PROVIDER_ID = "anthropic"
EVENT_ENTRYPOINT = "cloud"
SYNTHETIC_MODEL = "<synthetic>"
DEFAULT_SUBAGENT = "subagent"

_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
# `modelUsage` fields that are token counts; `costUSD` is a list-price estimate and is ignored, and
# `thinkingTokens` is a part of `outputTokens`, so adding it again would count it twice.
_MODEL_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
_ZERO = dict.fromkeys(_MODEL_FIELDS, 0)


def session_digest(session_id: str) -> str:
    """The stored stand-in for a cloud session id; the id itself is never stored or logged."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _slug(raw_model: str) -> str:
    """The normalized model id, without a context-window suffix.

    `modelUsage` keys a long-context run as `claude-opus-4-8[1m]`; the shared normalizer reads that as
    a different model (`opus-4`), so the messages and their usage would never meet.
    """
    return normalize_anthropic_model(raw_model.split("[", 1)[0])


def _message_key(message_id: str) -> str:
    return hashlib.sha256(message_id.encode()).hexdigest()[:12]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _strict_count(data: dict[str, Any], key: str) -> int:
    """A saved counter that ids or prices depend on: a bad value fails loudly instead of resetting."""
    value = data.get(key, 0)
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid reducer state: {key}")
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(*candidates: object) -> str | None:
    """The first parseable candidate as an ISO 8601 UTC string, else None."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            try:
                return parse_iso8601_utc(candidate).isoformat()
            except (ValueError, TypeError):
                continue
    return None


def _repo_name(value: str) -> str | None:
    """A repository name only: the last path segment, never a URL or an owner."""
    name = value.strip().rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return name or None


@dataclass(frozen=True)
class SessionFacts:
    """What the session list row says about the session (resolved by the caller)."""

    digest: str
    account_id: str
    repos: tuple[str, ...] = ()
    branch: str | None = None
    effort: str | None = None


@dataclass
class ReducerState:
    """What survives a closed turn. JSON-serialisable for the checkpoint."""

    previous_model_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    result_ordinal: int = 0
    cwd: str | None = None
    branch: str | None = None
    app_version: str | None = None
    ttl_1h: int = 0
    ttl_5m: int = 0
    # Short digests of the message ids already emitted: a resumed runner replays earlier assistant
    # messages in later turns, and those were not new calls.
    seen_messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReducerState:
        previous = {
            str(model): {name: _count(values.get(name)) for name in _MODEL_FIELDS}
            for model, values in (data.get("previous_model_usage") or {}).items()
            if isinstance(values, dict)
        }
        return cls(
            previous_model_usage=previous,
            result_ordinal=_strict_count(data, "result_ordinal"),
            cwd=_text(data.get("cwd")),
            branch=_text(data.get("branch")),
            app_version=_text(data.get("app_version")),
            ttl_1h=_strict_count(data, "ttl_1h"),
            ttl_5m=_strict_count(data, "ttl_5m"),
            seen_messages=[
                item for item in data.get("seen_messages") or [] if isinstance(item, str)
            ],
        )


@dataclass
class _Message:
    record: dict[str, Any]
    created_at: object
    cwd: str | None
    branch: str | None
    app_version: str | None


class ClaudeCloudHistoryReducer:
    """Feed ordered history events; collect the events of each closed turn.

    ``stats`` counts what was skipped or adjusted (``bad_timestamp``, ``synthetic_skipped``,
    ``negative_remainder``, ``restarts``, ``ttl_adjusted``) plus ``turns``, ``messages`` and
    ``remainders``; it carries counts only.
    """

    def __init__(self, facts: SessionFacts, state: ReducerState | None = None) -> None:
        self.facts = facts
        self.state = state or ReducerState()
        self.stats: Counter[str] = Counter()
        self._tail: dict[str, _Message] = {}
        self._seen = set(self.state.seen_messages)

    # -- input -------------------------------------------------------------------------------

    def feed(self, event: dict[str, Any]) -> list[UsageEventPush]:
        """One history record; the events of a turn when this record closes it, else nothing."""
        inner = event.get("payload")
        if not isinstance(inner, dict):
            return []
        kind = inner.get("type")
        if kind == "system":
            self._system(inner)
        elif kind == "assistant":
            self._assistant(event, inner)
        elif kind == "result":
            return self._close_turn(event, inner)
        return []

    def tail_events(self) -> list[UsageEventPush]:
        """Message events of the open turn (no remainders): for a session that went quiet.

        This deliberately does not add to ``_seen``: when the turn does close, ``_close_turn`` emits
        these messages again and needs their tokens for the remainder. The id dedupe is only for
        messages emitted by an earlier *closed* turn; recording them here would corrupt the
        remainders of an open turn.
        """
        return [
            push
            for message_id, held in self._tail.items()
            if _message_key(message_id) not in self._seen
            if (push := self._message_event(held))
        ]

    # -- records -----------------------------------------------------------------------------

    def _system(self, inner: dict[str, Any]) -> None:
        if cwd := _text(inner.get("cwd")):
            self.state.cwd = cwd
        if branch := _text(inner.get("branch")):
            self.state.branch = branch
        if inner.get("subtype") == "init" and (version := _text(inner.get("claude_code_version"))):
            self.state.app_version = version[:64]

    def _assistant(self, event: dict[str, Any], inner: dict[str, Any]) -> None:
        message = inner.get("message")
        message_id = _text(message.get("id")) if isinstance(message, dict) else None
        if message_id is None:
            return
        # Streaming repeats a message id; the last record wins and keeps its first position.
        self._tail[message_id] = _Message(
            record=inner,
            created_at=event.get("created_at"),
            cwd=self.state.cwd,
            branch=self.state.branch,
            app_version=self.state.app_version,
        )

    # -- project context ---------------------------------------------------------------------

    def _project(self, cwd: str | None) -> tuple[str | None, str | None]:
        """(project_ref, cwd) for an event.

        The working directory is only passed on when a repository is attributed: the ingestor would
        otherwise label the project with the last path segment, which is the wrong thing for a bare
        home or a parent directory.
        """
        names = tuple(name for name in map(_repo_name, self.facts.repos) if name)
        parts = [part for part in (cwd or "").split("/") if part]
        chosen: str | None = None
        if len(names) == 1 and (cwd is None or names[0] in parts):
            chosen = names[0]
        elif len(names) > 1:
            chosen = next((name for name in names if name in parts), None)
        return (chosen, cwd) if chosen else (None, None)

    def _base(self, context: _Message | None) -> dict[str, Any]:
        cwd = context.cwd if context else self.state.cwd
        branch = (context.branch if context else self.state.branch) or self.facts.branch
        project_ref, project_cwd = self._project(cwd)
        effort = self.facts.effort if self.facts.effort in _EFFORTS else None
        return {
            "provider_id": PROVIDER_ID,
            "account_id": self.facts.account_id,
            "source_app": CLAUDE_CODE,
            "session_id": self.facts.digest,
            "entrypoint": EVENT_ENTRYPOINT,
            "cwd": project_cwd,
            "project_ref": project_ref,
            "git_branch": branch,
            "effort": effort,
            "app_version": (context.app_version if context else self.state.app_version),
        }

    # -- events ------------------------------------------------------------------------------

    def _message_event(self, held: _Message) -> UsageEventPush | None:
        record = held.record
        message: dict[str, Any] = record.get("message") or {}
        raw_model = _text(message.get("model"))
        if raw_model is None:
            return None
        if raw_model == SYNTHETIC_MODEL:
            self.stats["synthetic_skipped"] += 1
            return None
        stamp = _timestamp(record.get("timestamp"), held.created_at)
        if stamp is None:
            self.stats["bad_timestamp"] += 1
            return None
        usage: dict[str, Any] = message["usage"] if isinstance(message.get("usage"), dict) else {}
        create_total = _count(usage.get("cache_creation_input_tokens"))
        create_1h = create_5m = 0
        split = usage.get("cache_creation")
        if isinstance(split, dict):
            create_1h = _count(split.get("ephemeral_1h_input_tokens"))
            create_5m = _count(split.get("ephemeral_5m_input_tokens"))
            if create_1h + create_5m != create_total:
                # The parts must add up to the total or the cost follows the parts.
                self.stats["ttl_adjusted"] += 1
                create_1h = min(create_1h, create_total)
                create_5m = create_total - create_1h
        tools = [
            block["name"]
            for block in message.get("content") or []
            if isinstance(block, dict)
            and block.get("type") == "tool_use"
            and _text(block.get("name"))
        ]
        subagent = None
        if record.get("parent_tool_use_id") is not None:
            subagent = _text(record.get("subagent_type")) or DEFAULT_SUBAGENT
        self.stats["messages"] += 1
        return UsageEventPush(
            event_id=f"cloud:{self.facts.digest}:m:{message['id']}",
            ts=stamp,
            model_id=_slug(raw_model),
            subagent_type=subagent,
            tool_names=tools or None,
            tool_calls=len(tools),
            tokens_input=_count(usage.get("input_tokens")),
            tokens_output=_count(usage.get("output_tokens")),
            tokens_cache_read=_count(usage.get("cache_read_input_tokens")),
            tokens_cache_create=create_total,
            tokens_cache_create_1h=create_1h,
            tokens_cache_create_5m=create_5m,
            stop_reason=_text(message.get("stop_reason")),
            service_tier=_text(usage.get("service_tier")),
            **self._base(held),
        )

    def _model_deltas(
        self, model_usage: object
    ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
        """How far each normalized model's cumulative usage moved since the last result.

        Also returns the new cumulative values per raw model key. The state is not touched: a turn
        that cannot be dated is skipped, and its usage must then still count at the next result.
        """
        deltas: dict[str, dict[str, int]] = {}
        latest: dict[str, dict[str, int]] = {}
        if not isinstance(model_usage, dict):
            return deltas, latest
        for raw_model, values in model_usage.items():
            if not isinstance(values, dict) or raw_model == SYNTHETIC_MODEL:
                continue
            current = {name: _count(values.get(name)) for name in _MODEL_FIELDS}
            previous = self.state.previous_model_usage.get(str(raw_model), _ZERO)
            if any(current[name] < previous[name] for name in _MODEL_FIELDS):
                # The runner process restarted: its counters began again from zero.
                self.stats["restarts"] += 1
                previous = _ZERO
            slug = _slug(str(raw_model))
            bucket = deltas.setdefault(slug, dict(_ZERO))
            for name in _MODEL_FIELDS:
                bucket[name] += current[name] - previous[name]
            latest[str(raw_model)] = current
        return deltas, latest

    def _close_turn(self, event: dict[str, Any], inner: dict[str, Any]) -> list[UsageEventPush]:
        self.state.result_ordinal += 1
        ordinal = self.state.result_ordinal
        self.stats["turns"] += 1
        events: list[UsageEventPush] = []
        have: dict[str, dict[str, int]] = {}  # every emitted message
        have_main: dict[str, dict[str, int]] = {}  # the same without subagent messages
        for message_id, entry in self._tail.items():
            key = _message_key(message_id)
            if key in self._seen:
                self.stats["repeated_message"] += 1
                continue
            push = self._message_event(entry)
            if push is None:
                continue
            self._seen.add(key)
            self.state.seen_messages.append(key)
            events.append(push)
            for bucket in (have, have_main) if push.subagent_type is None else (have,):
                total = bucket.setdefault(push.model_id or "", dict(_ZERO))
                total["inputTokens"] += push.tokens_input
                total["outputTokens"] += push.tokens_output
                total["cacheReadInputTokens"] += push.tokens_cache_read
                total["cacheCreationInputTokens"] += push.tokens_cache_create
            self.state.ttl_1h += push.tokens_cache_create_1h
            self.state.ttl_5m += push.tokens_cache_create_5m
        last_message_id = events[-1].event_id if events else None
        stamp = _timestamp(inner.get("timestamp"), event.get("created_at")) or (
            events[-1].ts if events else None
        )
        deltas, latest = self._model_deltas(inner.get("modelUsage"))
        self._tail = {}
        if stamp is None:
            self.stats["bad_timestamp"] += 1
            return events
        self.state.previous_model_usage.update(latest)
        for slug, delta in deltas.items():
            everything = have.get(slug, _ZERO)
            main = have_main.get(slug, _ZERO)
            gap = {name: delta[name] - everything[name] for name in _MODEL_FIELDS}
            if (
                any(value < 0 for value in gap.values())
                and main != everything
                and all(main[name] <= delta[name] for name in _MODEL_FIELDS)
            ):
                # Some runner versions leave subagent calls out of `modelUsage`: those messages are
                # real calls on top of it. Only when the main loop alone fits within it is the
                # overshoot theirs, so only the main loop is measured against it.
                self.stats["subagent_outside_model_usage"] += 1
                gap = {name: delta[name] - main[name] for name in _MODEL_FIELDS}
            elif any(value < 0 for value in gap.values()):
                self.stats["negative_remainder"] += 1
            rest = {name: max(value, 0) for name, value in gap.items()}
            if not any(rest.values()):
                continue
            create = rest["cacheCreationInputTokens"]
            seen = self.state.ttl_1h + self.state.ttl_5m
            create_5m: int | None = None
            if not self.state.seen_messages:
                create_1h = create  # no message yet to learn the split from: the observed norm
            elif seen == 0:
                create_1h = create_5m = (
                    0  # messages reported no split at all: unknown, as theirs is
                )
            else:
                create_1h = round(create * self.state.ttl_1h / seen)
            event_id = f"cloud:{self.facts.digest}:r:{ordinal}:{slug}"
            self.stats["remainders"] += 1
            events.append(
                UsageEventPush(
                    event_id=event_id,
                    ts=stamp,
                    model_id=slug,
                    turn_id=last_message_id or event_id,
                    tokens_input=rest["inputTokens"],
                    tokens_output=rest["outputTokens"],
                    tokens_cache_read=rest["cacheReadInputTokens"],
                    tokens_cache_create=create,
                    tokens_cache_create_1h=create_1h,
                    tokens_cache_create_5m=create - create_1h if create_5m is None else create_5m,
                    **self._base(None),
                )
            )
        return events
