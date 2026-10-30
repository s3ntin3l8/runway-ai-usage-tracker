"""Parse OpenCode SQLite database into UsageEventPush records.

OpenCode stores messages in a SQLite database. The default location is
`~/.local/share/opencode/opencode.db`, but some installs use the flatter
`~/.opencode/opencode.db` — the sidecar's `_discover_opencode_db_path` checks
both.

opencode v2 migrates this DB in place: messages move to `session_message`
(a `type` column, no `role`; model nested as `model.{id,providerID,variant}`;
cwd on `session_v2.directory`) and the v1 `message` table is left behind,
frozen. `session_v2` marks a v2 DB; `_normalize_v2_message` reshapes its rows
into the v1 form described below so one loop handles both. Message ids and
`time_created` survive the migration, so watermarks and event ids carry over.

Each assistant message row in the v1 `message` table has:
  - id: TEXT primary key (e.g. "msg_<hash>")
  - session_id: TEXT (direct column, not in JSON)
  - time_created: INTEGER (Unix milliseconds)
  - data: TEXT (JSON blob)

The data JSON contains:
  - role: "assistant" | "user"
  - modelID: model name string
  - providerID: "opencode-go" | "opencode" | "opencode-zen" | "open-design-byok"
    | "openrouter" | "ollama-cloud" | ... (OpenCode's own backend/billing-tier tag — NOT the
    upstream model maker)
  - cost: float (USD — authoritative; skip pricing table lookup)
  - tokens: {input, output, reasoning, cache: {read, write}, total}
  - finish: stop reason
  - variant: "high" | "medium" | absent — per-message intensity level, mapped
    to usage_events.effort (session.model.variant is session-level only and
    is not a per-turn source)
  - error: {name, data: {message, statusCode, ...}} — present when the
    request to the upstream backend failed (no tokens/cost were incurred)

Because OpenCode logs the cost directly per message, these events carry
cost_usd and the server's EventIngestor should use that value rather than
computing from the pricing table.

providerID -> runway provider_id mapping (see _OC_PROVIDER_MAP below):
  - "opencode"          -> "opencode-free"      (free-tier models)
  - "opencode-go"       -> "opencode"           (paid Go subscription; carries
                                                   the web-scraper quota gauges;
                                                   deepseek-v4-flash/pro served
                                                   from the Go tier stay here —
                                                   they are billed to the
                                                   subscription, not to a
                                                   DeepSeek balance)
  - "opencode-zen"      -> "opencode-zen"       (Zen pay-as-you-go tier)
  - "open-design-byok"  -> "opencode-byok"      (bring-your-own-key)
  - "openrouter"        -> "opencode-openrouter"
  - "ollama-cloud"      -> "opencode-ollama"    (folded onto "ollama" by
                                                 _OC_CANONICAL_MAP below)
  - "deepseek"          -> "opencode-deepseek"  (folded onto the direct
                                                 "deepseek" provider by
                                                 _OC_CANONICAL_MAP below —
                                                 BYOK keys are pay-as-you-go
                                                 against the DeepSeek balance)
  - anything else       -> "opencode-<slug>"    (never silently folds into Go)
  - missing/empty       -> "opencode"           (historical default)

A second map, _OC_CANONICAL_MAP, catches providerIDs that front a provider
Runway already collects directly (e.g. MiniMax's coding plan, reachable both
through its own API key and through OpenCode). Those events are retagged onto
the canonical provider_id. The map value is (canonical provider_id,
account_id override): a concrete account_id forces every event onto that
account (e.g. MiniMax's API-key-only collector only ever emits
account_id="default"); None means OpenCode does not identify which upstream
credential handled the message. An exact provider-level mapping can assign it;
otherwise it stays on the `default` account for manual assignment. Fingerprint
and path hints identify one credential but cannot be joined to an OpenCode
message. Their logged `cost` is dropped (cost_usd=None) so the server prices
them from provider_pricing instead of trusting a subscription's $0 — see
cost_calculator.compute_event_cost_breakdown.

Messages whose `error` field is set are pushed with kind="error" (no tokens/
cost were actually incurred) so they don't inflate usage totals on whichever
card they land on.
"""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.source_apps import OPENCODE  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.model_normalization import is_openrouter_model_id  # noqa: E402
from scripts.sidecar_pkg.canonical_providers import (  # noqa: E402
    SHARED_CANONICAL_PROVIDER_MAP,
    CanonicalProviderTuple,
)
from scripts.sidecar_pkg.session_names import make_record  # noqa: E402
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor  # noqa: E402

# OpenCode's own providerID (backend/billing tier) -> runway provider_id.
# Keep this in sync with scripts/reclassify_opencode_providers.py, which
# reapplies the same mapping to already-ingested events.
_OC_PROVIDER_MAP: dict[str, str] = {
    "opencode": "opencode-free",
    "opencode-go": "opencode",
    "opencode-zen": "opencode-zen",
    "open-design-byok": "opencode-byok",
    "openrouter": "opencode-openrouter",
    "ollama-cloud": "opencode-ollama",
}


# providerIDs that name an OpenCode gateway rather than a backend: the three provider
# ids the Data Health `openrouter_model_events` check scans (opencode-free, opencode,
# opencode-byok) come from exactly these.
_OC_GATEWAY_PROVIDER_IDS = frozenset({"", "opencode", "opencode-go", "open-design-byok"})


def map_opencode_provider_id(oc_provider_id: str) -> str:
    """Map an OpenCode `providerID` to a runway `provider_id`.

    Unknown/new backends get their own derived `opencode-<slug>` id rather
    than silently collapsing into the Go tier (the bug in issue #182). A
    missing/empty providerID keeps the historical default of "opencode".
    """
    oc_provider_id = (oc_provider_id or "").strip().lower()
    if not oc_provider_id:
        return "opencode"
    return _OC_PROVIDER_MAP.get(oc_provider_id, f"opencode-{oc_provider_id}")


# OpenCode providerIDs that front a provider Runway already collects directly
# -> (canonical provider_id, explicit account override or None). With no
# override, only an exact provider-level mapping can identify the account.
# Fingerprint and path tags identify a particular credential, but OpenCode
# does not report which credential handled a message. Unmatched events stay
# pending for assignment.
# Keep this in sync with scripts/reclassify_opencode_providers.py.
_OC_CANONICAL_MAP: dict[str, CanonicalProviderTuple] = {
    **SHARED_CANONICAL_PROVIDER_MAP,
    # Kimi For Coding (kimi-code-plan-global backend in OpenCode).
    "kimi-code-plan-global": ("kimi_coding", None),
}


def map_opencode_canonical(oc_provider_id: str) -> tuple[str, str | None] | None:
    """Return the canonical provider and optional explicit account override."""
    return _OC_CANONICAL_MAP.get((oc_provider_id or "").strip().lower())


def _canonical_for(oc_provider_id: str, model_id: str) -> CanonicalProviderTuple | None:
    """The canonical provider an OpenCode message is retagged onto, if any.

    ``_OC_CANONICAL_MAP`` first; otherwise a generic gateway tag (``opencode``/BYOK)
    on a model id that only OpenRouter serves (``vendor/model:free``,
    ``openrouter/...``) folds like a message tagged ``providerID="openrouter"``.
    Limited to the generic gateways so a local/other backend's own ids are never claimed.
    """
    canonical = map_opencode_canonical(oc_provider_id)
    if (
        canonical is None
        and oc_provider_id in _OC_GATEWAY_PROVIDER_IDS
        and is_openrouter_model_id(model_id)
    ):
        canonical = map_opencode_canonical("openrouter")
    return canonical


def event_provider_id(oc_provider_id: str, model_id: str) -> str:
    """The runway ``provider_id`` the extractor stamps on a message's event."""
    canonical = _canonical_for(oc_provider_id, model_id)
    if canonical is not None:
        return canonical[0]
    return map_opencode_provider_id(oc_provider_id)


def _classify_opencode_error(err: dict) -> str:
    """Best-effort short tag for an OpenCode message-level error.

    `err` is the raw `data.error` dict, e.g.
    `{"name": "APIError", "data": {"statusCode": 401, "message": "..."}}`.
    """
    status = None
    err_data = err.get("data")
    if isinstance(err_data, dict):
        status = err_data.get("statusCode")

    if status == 401:
        return "auth_failed"
    if status == 403:
        return "quota_exceeded"
    if status == 429:
        return "rate_limit"
    if status in (408, 504):
        return "timeout"
    if isinstance(status, int):
        return f"http_{status}"

    name = err.get("name")
    return str(name).lower() if name else "unknown_error"


_V1_QUERY = """
    SELECT id, session_id, time_created, data
    FROM message
    WHERE time_created > ?
      AND json_extract(data, '$.role') = 'assistant'
    ORDER BY time_created ASC
"""

# opencode v2 moved messages into ``session_message`` (type column, no ``role``) and
# left the v1 ``message`` table behind, frozen. cwd now lives on the session row.
_V2_QUERY = """
    SELECT m.id, m.session_id, m.time_created, m.type, m.data, s.directory, s.model
    FROM session_message m
    LEFT JOIN session_v2 s ON s.id = m.session_id
    WHERE m.time_created > ?
      AND m.type IN ('assistant', 'compaction')
    ORDER BY m.time_created ASC, m.id ASC
"""

# A row still streaming longer than this is treated as abandoned (crash / killed
# session) rather than holding the whole watermark back indefinitely.
_V2_PENDING_GRACE_MS = 60 * 60 * 1000


def _is_v2(cur: sqlite3.Cursor) -> bool:
    """True when the DB has been migrated to opencode v2.

    ``session_v2`` is the signal: opencode 1.19 already ships an (empty)
    ``session_message`` table, so that one does not distinguish the majors.
    """
    cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_v2'")
    return cur.fetchone() is not None


def _v2_row_status(raw: dict, msg_type: str, time_created_ms: int, now_ms: int) -> str:
    """Classify a v2 row as ``ready``, ``pending`` (hold back) or ``skip``.

    ``pending`` stalls the whole forward window by design (the caller ``break``s,
    not ``continue``s): ingest is insert-or-ignore and the watermark advances past
    every pushed event, so anything after an in-flight row must wait too.
    ``skip`` (unsettled for over an hour) is a known trade-off: if such a row
    later settles — e.g. a long compaction — its ``time_created`` is already behind
    the watermark and it is not recovered.
    """
    if msg_type == "compaction":
        settled = raw.get("status") in ("completed", "failed")
    else:
        time_obj = raw.get("time") or {}
        settled = time_obj.get("completed") is not None or isinstance(raw.get("error"), dict)
    if settled:
        return "ready"
    return "pending" if now_ms - int(time_created_ms) < _V2_PENDING_GRACE_MS else "skip"


def _has_usage(raw: dict) -> bool:
    """True when a v2 payload recorded any cost or a non-zero token count."""
    if raw.get("cost"):
        return True
    tokens = raw.get("tokens") or {}
    cache = tokens.get("cache") or {}
    counts = [tokens.get(k) for k in ("input", "output", "reasoning")]
    counts += [cache.get("read"), cache.get("write")]
    return any(isinstance(n, int | float) and n > 0 for n in counts)


def _normalize_v2_message(
    raw: dict, msg_type: str, directory: str | None, session_model: dict | None
) -> dict | None:
    """Reshape a v2 ``session_message`` payload into the v1 ``message.data`` form.

    Lets the shared row loop below stay the single place that maps provider ids,
    canonical retags, errors and effort. Returns None for rows that carry no usage
    (a compaction with no cost and no non-zero token count).
    """
    if msg_type == "compaction" and not _has_usage(raw):
        return None
    # Compactions only sometimes name the model; fall back to the session's.
    model = (raw["model"] if "model" in raw else session_model) or {}
    variant = model.get("variant")
    # Migrated v1 rows get variant "default" where v1 had none — that is "no effort".
    if isinstance(variant, str) and variant.strip().lower() == "default":
        variant = None
    data: dict = {
        "modelID": model.get("id"),
        "providerID": model.get("providerID"),
        "variant": variant,
        "tokens": raw.get("tokens"),
        "cost": raw.get("cost"),
        "finish": raw.get("finish"),
        "error": raw.get("error"),
        "time": raw.get("time"),
    }
    if directory:
        data["path"] = {"cwd": directory}
    return data


def parse_opencode_events(
    db_path: Path,
    account_id: str,
    since: datetime,
    canonical_hints: dict[str, dict[str, str]] | None = None,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from an OpenCode SQLite database.

    Reads all assistant messages with time_created > since from the message table.
    Deduplicates by message id (the primary key is stable).
    Sets cost_usd from the logged cost field (skip pricing table lookup), except
    for providerIDs in _OC_CANONICAL_MAP, where cost_usd is left None so the
    server prices the event from provider_pricing instead.

    Args:
        db_path: Path to the opencode.db SQLite file.
        account_id: Canonical account email or "default".
        since: Only return events strictly after this timestamp.
        canonical_hints: Optional ``{canonical_provider_id: {origin:
            account_id, ...}}`` map sourced from the server's
            ``account_tag_hints`` payload. An exact provider-level mapping
            can assign events to an account; fingerprint and path origins
            cannot be joined to a specific message. This lookup happens after
            the ``_OC_CANONICAL_MAP`` retag decision is known — the events
            branch's iterating-provider hint lookup
            (``run_collection``) can't see canonical-provider hints
            because it keys on the iterating provider (e.g.
            ``provider:opencode``), but the server emits hints under
            the canonical key (e.g. ``provider:minimax``) via
            ``CredentialTagRepo.auto_hints_for_single_account_providers``.
            Without this merge, an event retagged to ``minimax`` lands
            at ``(minimax, "default")`` even when the operator has a
            configured ``s3ntin318@gmail.com`` row.
    """
    if not db_path.exists():
        return []

    # Convert since to millisecond epoch for SQLite comparison
    since_ms = int(since.timestamp() * 1000)

    events: list[UsageEventPush] = []
    try:
        # Read-only: opencode keeps this DB open (WAL) from a long-lived process,
        # and we only ever select from it.
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        return []

    try:
        try:
            cur = conn.cursor()
            v2 = _is_v2(cur)
            if v2:
                cur.execute(_V2_QUERY, (since_ms,))
            else:
                cur.execute(_V1_QUERY, (since_ms,))
            rows = iter_cursor(cur)
        except Exception:
            return []

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        try:
            for row in rows:
                if v2:
                    (
                        msg_id,
                        session_id,
                        time_created_ms,
                        msg_type,
                        data_json,
                        directory,
                        model_json,
                    ) = row
                    try:
                        raw = json.loads(data_json) if data_json else {}
                        session_model = json.loads(model_json) if model_json else None
                    except Exception:
                        continue
                    status = _v2_row_status(raw, msg_type, time_created_ms, now_ms)
                    if status == "pending":
                        # Still streaming. Ingest is insert-or-ignore and the watermark
                        # advances past this row, so pushing it now would freeze a
                        # partial row forever — hold here until it settles.
                        break
                    if status == "skip":
                        continue
                    data = _normalize_v2_message(raw, msg_type, directory, session_model)
                    if data is None:
                        continue
                else:
                    msg_id, session_id, time_created_ms, data_json = row
                    try:
                        data = json.loads(data_json) if data_json else {}
                    except Exception:
                        continue

                # Convert ms epoch to datetime
                try:
                    ts = datetime.fromtimestamp(int(time_created_ms) / 1000.0, tz=UTC)
                except Exception:
                    continue

                # Extract token fields from the nested tokens dict
                raw_tokens = data.get("tokens") or {}
                tokens_input = int(raw_tokens.get("input", 0))
                tokens_output = int(raw_tokens.get("output", 0))
                tokens_reasoning = int(raw_tokens.get("reasoning", 0))
                cache = raw_tokens.get("cache") or {}
                tokens_cache_read = int(cache.get("read", 0))
                tokens_cache_create = int(cache.get("write", 0))

                # Cost is authoritative — skip pricing table
                cost_usd = float(data.get("cost") or 0.0)

                model_id = data.get("modelID") or "unknown"
                stop_reason = data.get("finish") or None
                # Per-message intensity (OpenCode's `variant`) → effort. Absent → None.
                # Normalize: non-strings are dropped (UsageEventPush would ValidationError),
                # and casing/whitespace are canonicalized to the documented lowercase form.
                raw_effort = data.get("variant")
                effort = (
                    raw_effort.strip().lower()
                    if isinstance(raw_effort, str) and raw_effort.strip()
                    else None
                )

                # Working directory (path.cwd, falling back to the repo root) and request
                # latency (completed − created, both ms epoch) — OpenCode is the only
                # provider that logs message timing.
                path = data.get("path") or {}
                cwd = path.get("cwd") or path.get("root")
                time_obj = data.get("time") or {}
                created = time_obj.get("created")
                completed = time_obj.get("completed")
                latency_ms = (
                    int(completed - created)
                    if isinstance(created, int | float)
                    and isinstance(completed, int | float)
                    and completed >= created
                    else None
                )

                # Use the row's id (stable primary key) as event_id
                event_id = msg_id or f"opencode|{session_id or 'unknown'}|ts_{time_created_ms}"

                # OpenCode tags each message with a providerID identifying which
                # backend/billing tier served it (Go subscription, free models,
                # bring-your-own-key, OpenRouter, Ollama Cloud, ...). Map it to a
                # dedicated runway provider_id so no backend is ever silently folded
                # into the Go tier (issue #182).
                oc_provider_id = data.get("providerID") or ""
                runway_provider_id = map_opencode_provider_id(oc_provider_id)
                event_account_id = account_id

                # Some OpenCode providerIDs front a provider Runway already collects
                # directly (e.g. MiniMax's coding plan) — retag onto that provider_id
                # (and its account_id when the map pins one) so this event lands on the
                # same card, and drop the logged $0 subscription cost so the server
                # prices it. A None account override falls back to the operator's
                # tag-hint for the canonical provider (when supplied) — the events
                # branch in run_collection only ships the iterating provider's hint
                # (e.g. provider:opencode), but the server emits the auto-hint under
                # the canonical provider (e.g. provider:minimax), so per-event
                # consultation here is the only way to retarget the event onto the
                # labeled quota card.
                canonical = _canonical_for(oc_provider_id, model_id)
                if canonical is not None:
                    canonical_provider_id, account_override = canonical
                    runway_provider_id = canonical_provider_id
                    if account_override is not None:
                        event_account_id = account_override
                        event_account_source = "tag"
                    elif canonical_hints:
                        provider_hints = canonical_hints.get(canonical_provider_id, {})
                        # OpenCode only tells us which upstream provider served a
                        # message, not which credential origin it used. A fingerprint
                        # or path tag describes one credential and cannot prove it
                        # handled this message; only a provider-level mapping can.
                        canonical_hint = provider_hints.get(f"provider:{canonical_provider_id}")
                        if canonical_hint:
                            event_account_id = canonical_hint
                            event_account_source = "tag"
                        else:
                            # The account identity used for the OpenCode provider
                            # does not prove which account owns a message billed by
                            # an underlying provider such as xAI or OpenRouter.
                            event_account_id = "default"
                            event_account_source = "default"
                    else:
                        # The OpenCode account identity identifies the OpenCode user,
                        # not which account they used through a canonical backend.
                        # Keep this event pending until the canonical provider is
                        # explicitly mapped to one of its configured accounts.
                        event_account_id = "default"
                        event_account_source = "default"
                    cost_usd = None
                else:
                    event_account_source = None

                # A failed request (bad auth, no subscription, etc.) never actually
                # incurred usage — push it as kind="error" so it doesn't inflate
                # message/token counts on whichever card it lands on.
                err = data.get("error")
                if isinstance(err, dict):
                    events.append(
                        UsageEventPush(
                            provider_id=runway_provider_id,
                            source_app=OPENCODE,
                            account_id=event_account_id,
                            account_source=event_account_source,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            model_id=model_id,
                            session_id=session_id or None,
                            cwd=cwd,
                            kind="error",
                            error_reason=_classify_opencode_error(err),
                        )
                    )
                    continue

                events.append(
                    UsageEventPush(
                        provider_id=runway_provider_id,
                        source_app=OPENCODE,
                        account_id=event_account_id,
                        account_source=event_account_source,
                        event_id=event_id,
                        ts=ts.isoformat(),
                        model_id=model_id,
                        session_id=session_id or None,
                        cwd=cwd,
                        latency_ms=latency_ms,
                        tokens_input=tokens_input,
                        tokens_output=tokens_output,
                        tokens_cache_read=tokens_cache_read,
                        tokens_cache_create=tokens_cache_create,
                        tokens_reasoning=tokens_reasoning,
                        stop_reason=stop_reason,
                        tool_calls=0,
                        effort=effort,
                        cost_usd=cost_usd,
                    )
                )
        except CursorReadError:
            return []
    finally:
        conn.close()

    return events


_SESSION_TABLES = ("session_v2", "session")


def _titled_sessions(cur: sqlite3.Cursor, v2: bool) -> dict[str, str]:
    """``{session_id: title}`` for sessions with a non-blank title, newest first."""
    titles: dict[str, str] = {}
    for table in _SESSION_TABLES if v2 else ("session",):
        columns = {info[1] for info in cur.execute(f"PRAGMA table_info({table})")}
        if not {"id", "title"} <= columns:
            continue
        order = " ORDER BY time_updated DESC" if "time_updated" in columns else ""
        for session_id, title in cur.execute(f"SELECT id, title FROM {table}{order}"):
            if not isinstance(session_id, str) or not isinstance(title, str):
                continue
            if session_id and title.strip():
                titles.setdefault(session_id, title.strip())
    return titles


def parse_opencode_session_names(db_path: Path) -> list[dict]:
    """Session titles for the sessions the usage extractor reports events for.

    One ``generated`` record per (provider id, session): a session whose messages
    were served by several backends is retagged per message, so its events (and
    therefore its name) live under several runway provider ids. Only ``title`` is
    read; no message text. Read-only, like the event extraction.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        return []
    try:
        cur = conn.cursor()
        v2 = _is_v2(cur)
        titles = _titled_sessions(cur, v2)
        if not titles:
            return []
        if v2:
            cur.execute(
                "SELECT DISTINCT m.session_id, json_extract(m.data, '$.model'), s.model "
                "FROM session_message m LEFT JOIN session_v2 s ON s.id = m.session_id "
                "WHERE m.type IN ('assistant', 'compaction')"
            )
        else:
            cur.execute(
                "SELECT DISTINCT session_id, json_extract(data, '$.providerID'), "
                "json_extract(data, '$.modelID') FROM message "
                "WHERE json_extract(data, '$.role') = 'assistant'"
            )
        provider_ids: dict[str, set[str]] = {}
        for session_id, first, second in iter_cursor(cur):
            if session_id not in titles:
                continue
            if v2:
                try:
                    model = json.loads(first) if first else (json.loads(second) if second else {})
                except Exception:
                    continue
                model = model if isinstance(model, dict) else {}
                oc_provider = model.get("providerID") or ""
                model_id = model.get("id") or "unknown"
            else:
                oc_provider = first or ""
                model_id = second or "unknown"
            provider_ids.setdefault(session_id, set()).add(
                event_provider_id(str(oc_provider), str(model_id))
            )
    except Exception:
        return []
    finally:
        conn.close()
    records: list[dict] = []
    for session_id, title in titles.items():
        for provider_id in sorted(provider_ids.get(session_id, ())):
            records.append(make_record(provider_id, session_id, title, "generated", OPENCODE))
    return records
