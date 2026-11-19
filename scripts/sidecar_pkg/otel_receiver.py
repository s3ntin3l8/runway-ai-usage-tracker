"""Loopback OTLP/HTTP-JSON receiver for Claude Code ``api_request`` log events (#1365).

Claude Code makes model calls its transcripts never record (session titles,
summaries, compaction, prompt suggestions, ...). Its OpenTelemetry
``api_request`` log events report every call with a ``request_id`` and token
counts. This module listens on ``127.0.0.1:4318`` for ``POST /v1/logs``, keeps
only the calls the transcripts lack and queues them as ``UsageEventPush`` for
the sidecar's normal push path. The server prices them from tokens.

Privacy contract:

* Only the attributes in ``_ATTRIBUTES`` are ever read out of a record. Every
  other attribute -- ``cost_usd``, the user's e-mail, account and organisation
  ids, prompt text -- is discarded while parsing and never stored.
* No attribute value is logged, only counts.

Resource contract: binds the loopback interface only, bodies are size-capped
and the queue is bounded (oldest dropped, drops counted). A port that cannot
be bound costs one warning, never the sidecar.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.core.source_apps import CLAUDE_CODE
from app.models.schemas import UsageEventPush
from app.services.model_normalization import normalize_anthropic_model

#: OTLP/HTTP's standard port. The bind address is not configurable.
DEFAULT_PORT = 4318
BIND_HOST = "127.0.0.1"
LOGS_PATH = "/v1/logs"
#: Event-id prefix of captured events; the sidecar keys watermark handling on it.
EVENT_ID_PREFIX = "otel|"

#: Largest request body accepted; a larger one is refused before it is read.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: Events held awaiting the next push; the oldest are dropped beyond this.
QUEUE_CAP = 5000

#: The only attributes ever read from a log record.
_STRING_ATTRIBUTES = frozenset(
    {"model", "request_id", "query_source", "speed", "effort", "session.id", "event.timestamp"}
)
_TOKEN_ATTRIBUTES = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cache_creation_5m_tokens",
        "cache_creation_1h_tokens",
    }
)
_ATTRIBUTES = _STRING_ATTRIBUTES | _TOKEN_ATTRIBUTES
#: Read only to decide whether a record is an ``api_request``; never stored.
_EVENT_NAME = "event.name"
_API_REQUEST = "api_request"

#: The transcripts already record these calls.
_MAIN_THREAD_SOURCE = "repl_main_thread"
_AGENT_PREFIX = "agent:"

_FIELD_MAX_LENGTH = 128


def _scalar(value: Any) -> Any:
    """The scalar inside an OTLP ``AnyValue`` (``stringValue``, ``intValue``, ...)."""
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(char for char in value if char.isprintable()).strip()
    return cleaned[:_FIELD_MAX_LENGTH] or None


def _tokens(value: Any) -> int:
    """A token count (OTLP JSON sends int64 as a string); anything else is 0."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _is_covered_by_transcripts(query_source: str | None) -> bool:
    return query_source is not None and (
        query_source == _MAIN_THREAD_SOURCE or query_source.startswith(_AGENT_PREFIX)
    )


def _api_request_attributes(record: Any) -> dict[str, Any] | None:
    """The allow-listed attributes of an ``api_request`` record, else ``None``."""
    if not isinstance(record, dict):
        return None
    attributes: dict[str, Any] = {}
    event_name = record.get("eventName")
    for item in record.get("attributes") or ():
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if key == _EVENT_NAME:
            name = _scalar(item.get("value"))
            if isinstance(name, str):
                event_name = name
        elif key in _ATTRIBUTES:
            attributes[key] = _scalar(item.get("value"))
    if not isinstance(event_name, str):
        return None
    # Claude Code names the event ``api_request`` and may prefix it with the
    # product (``claude_code.api_request``).
    if event_name != _API_REQUEST and not event_name.endswith("." + _API_REQUEST):
        return None
    return attributes


class ParseResult:
    """Events from one request body and what was left out of it."""

    __slots__ = ("events", "filtered", "incomplete")

    def __init__(self) -> None:
        self.events: list[UsageEventPush] = []
        self.filtered = 0  # covered by the transcripts
        self.incomplete = 0  # no request_id or no usable timestamp


def parse_logs(document: Any) -> ParseResult:
    """Turn an OTLP logs document into the events the transcripts lack."""
    result = ParseResult()
    if not isinstance(document, dict):
        return result
    for resource in document.get("resourceLogs") or ():
        for scope in (resource.get("scopeLogs") or ()) if isinstance(resource, dict) else ():
            for record in (scope.get("logRecords") or ()) if isinstance(scope, dict) else ():
                attributes = _api_request_attributes(record)
                if attributes is None:
                    continue
                query_source = _text(attributes.get("query_source"))
                if _is_covered_by_transcripts(query_source):
                    result.filtered += 1
                    continue
                request_id = _text(attributes.get("request_id"))
                ts = _timestamp(_text(attributes.get("event.timestamp")))
                if request_id is None or ts is None:
                    result.incomplete += 1
                    continue
                result.events.append(
                    UsageEventPush(
                        provider_id="anthropic",
                        account_id="default",  # stamped by ``drain``
                        source_app=CLAUDE_CODE,
                        event_id=f"{EVENT_ID_PREFIX}{request_id}",
                        request_id=request_id,
                        query_source=query_source,
                        ts=ts,
                        model_id=normalize_anthropic_model(_text(attributes.get("model")) or ""),
                        session_id=_text(attributes.get("session.id")),
                        tokens_input=_tokens(attributes.get("input_tokens")),
                        tokens_output=_tokens(attributes.get("output_tokens")),
                        tokens_cache_read=_tokens(attributes.get("cache_read_tokens")),
                        tokens_cache_create=_tokens(attributes.get("cache_creation_tokens")),
                        tokens_cache_create_1h=_tokens(attributes.get("cache_creation_1h_tokens")),
                        tokens_cache_create_5m=_tokens(attributes.get("cache_creation_5m_tokens")),
                        speed=_text(attributes.get("speed")),
                        effort=_text(attributes.get("effort")),
                    )
                )
    return result


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows lets SO_REUSEADDR share a live listener's port, which would let
    # a second receiver "bind" and silently steal traffic.
    allow_reuse_address = os.name != "nt"
    receiver: OtelReceiver


#: Seconds a client may stall on a connection before its handler gives up.
CLIENT_TIMEOUT = 30


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    # Applied by ``StreamRequestHandler.setup``: a client that sends a
    # ``Content-Length`` and then stalls cannot pin its handler thread.
    timeout = CLIENT_TIMEOUT

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log; nothing about a request is logged."""

    def _reply(self, status: int, body: bytes = b"{}") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != LOGS_PATH:
            self._reply(404)
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" or self.headers.get("Content-Encoding"):
            self._reply(415)
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._reply(411)
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._reply(413)
            return
        try:
            document = json.loads(self.rfile.read(length))
        except (ValueError, OSError):  # includes a read timeout
            self._reply(400)
            return
        self.server.receiver.accept(parse_logs(document))
        self._reply(200)

    def _not_found(self) -> None:
        self._reply(404)

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_found  # noqa: N815


class OtelReceiver:
    """Queue of captured events plus the loopback server that fills it."""

    def __init__(self, port: int = DEFAULT_PORT, queue_cap: int = QUEUE_CAP) -> None:
        self._port = port
        self._cap = queue_cap
        self._lock = threading.Lock()
        self._queue: deque[UsageEventPush] = deque()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.dropped_overflow = 0
        self.dropped_incomplete = 0
        self.filtered = 0
        self._logged = (0, 0, 0)  # filtered, incomplete, overflow at the last drain

    @property
    def address(self) -> tuple[str, int] | None:
        """The bound ``(host, port)``, or ``None`` while not listening."""
        server = self._server
        if server is None:
            return None
        host, port = server.server_address[:2]
        return str(host), int(port)

    def start(self) -> bool:
        """Bind the loopback port and serve in a thread. Never raises."""
        if self._server is not None:
            return True
        try:
            server = _Server((BIND_HOST, self._port), _Handler)
        except OSError as exc:
            logging.warning(
                "Claude Code telemetry receiver not started: cannot bind %s:%s (%s)",
                BIND_HOST,
                self._port,
                type(exc).__name__,
            )
            return False
        server.receiver = self
        thread = threading.Thread(target=server.serve_forever, name="OtelReceiver", daemon=True)
        self._server = server
        self._thread = thread
        try:
            thread.start()
        except RuntimeError as exc:  # e.g. no threads left to spawn
            self._server = self._thread = None
            server.server_close()
            logging.warning(
                "Claude Code telemetry receiver not started: cannot spawn its thread (%s)",
                type(exc).__name__,
            )
            return False
        logging.info("Claude Code telemetry receiver listening on %s:%s", *self.address or ())
        return True

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        try:
            # ``shutdown()`` waits for ``serve_forever`` to exit, so it would
            # block forever if the serve thread never ran.
            if thread is not None and thread.is_alive():
                server.shutdown()
        except Exception:
            logging.debug("telemetry receiver shutdown failed", exc_info=True)
        try:
            server.server_close()
        except Exception:
            logging.debug("telemetry receiver close failed", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def accept(self, parsed: ParseResult) -> None:
        with self._lock:
            self.filtered += parsed.filtered
            self.dropped_incomplete += parsed.incomplete
            for event in parsed.events:
                self._append(event)

    def _append(self, event: UsageEventPush) -> None:
        if len(self._queue) >= self._cap:
            self._queue.popleft()
            self.dropped_overflow += 1
        self._queue.append(event)

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain(self, account_id: str, account_source: str | None = None) -> list[dict[str, Any]]:
        """Take every queued event, stamped with ``account_id``, as push payloads."""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            totals = (self.filtered, self.dropped_incomplete, self.dropped_overflow)
            filtered, incomplete, overflow = (
                now - before for now, before in zip(totals, self._logged, strict=True)
            )
            self._logged = totals
        if events or filtered or incomplete or overflow:
            # Counts only, never values; since the previous drain.
            logging.debug(
                "telemetry: drained %d, filtered %d, dropped %d (incomplete), %d (overflow)",
                len(events),
                filtered,
                incomplete,
                overflow,
            )
        payloads = []
        for event in events:
            payload = event.model_copy(update={"account_id": account_id}).model_dump(mode="json")
            if account_source:
                payload["account_source"] = account_source
            payloads.append(payload)
        return payloads

    def requeue(self, payloads: list[dict[str, Any]]) -> None:
        """Put events back after a failed push; other providers' events are ignored."""
        events = []
        for payload in payloads:
            if str(payload.get("event_id", "")).startswith(EVENT_ID_PREFIX):
                try:
                    events.append(UsageEventPush.model_validate(payload))
                except ValueError:
                    continue
        with self._lock:
            # Older first, so the oldest are the ones dropped on overflow.
            pending = list(self._queue)
            self._queue.clear()
            for event in [*events, *pending]:
                self._append(event)


#: The sidecar's receiver. Created lazily so importing this module binds nothing.
_receiver: OtelReceiver | None = None


def get_receiver() -> OtelReceiver:
    global _receiver
    if _receiver is None:
        _receiver = OtelReceiver()
    return _receiver


def start_receiver() -> bool:
    return get_receiver().start()


def stop_receiver() -> None:
    if _receiver is not None:
        _receiver.stop()
