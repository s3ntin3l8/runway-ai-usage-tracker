"""Parse OpenCode SQLite database into UsageEventPush records.

OpenCode stores messages in a SQLite database. The default location is
`~/.local/share/opencode/opencode.db`, but some installs use the flatter
`~/.opencode/opencode.db` — the sidecar's `_discover_opencode_db_path` checks
both.

Each assistant message row in the `message` table has:
  - id: TEXT primary key (e.g. "msg_<hash>")
  - session_id: TEXT (direct column, not in JSON)
  - time_created: INTEGER (Unix milliseconds)
  - data: TEXT (JSON blob)

The data JSON contains:
  - role: "assistant" | "user"
  - modelID: model name string
  - providerID: "opencode-go" | "opencode" | "open-design-byok" | "openrouter"
    | "ollama-cloud" | ... (OpenCode's own backend/billing-tier tag — NOT the
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
account_id="default"); None keeps the event's own account_id — what OpenCode
already resolved (usually the user's email) — which matches the account a
user-labeled collector card resolves to. Their logged `cost` is dropped
(cost_usd=None) so the server prices them from provider_pricing instead of
trusting a subscription's $0 — see cost_calculator.compute_event_cost_breakdown.

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

from app.models.schemas import UsageEventPush  # noqa: E402

# OpenCode's own providerID (backend/billing tier) -> runway provider_id.
# Keep this in sync with scripts/reclassify_opencode_providers.py, which
# reapplies the same mapping to already-ingested events.
_OC_PROVIDER_MAP: dict[str, str] = {
    "opencode": "opencode-free",
    "opencode-go": "opencode",
    "open-design-byok": "opencode-byok",
    "openrouter": "opencode-openrouter",
    "ollama-cloud": "opencode-ollama",
}


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
# override, a canonical provider's unambiguous credential tag must identify
# the account; OpenCode's user identity is not evidence about which upstream
# account handled the request. Unmatched events stay pending for assignment.
# Keep this in sync with scripts/reclassify_opencode_providers.py.
_OC_CANONICAL_MAP: dict[str, tuple[str, str | None]] = {
    # MiniMax coding plan exposes no provider account identity in OpenCode.
    "minimax-coding-plan": ("minimax", None),
    # Kimi For Coding (kimi-code-plan-global backend in OpenCode).
    "kimi-code-plan-global": ("kimi_coding", None),
    # Ollama Cloud.
    "ollama-cloud": ("ollama", None),
    # OpenRouter.
    "openrouter": ("openrouter", None),
    # xAI.
    "xai": ("xai", None),
    # DeepSeek — BYOK keys inside OpenCode (providerID "deepseek") are
    # pay-as-you-go against the DeepSeek prepaid balance, so retag onto the
    # direct "deepseek" provider: its quota card comes from
    # GET api.deepseek.com/user/balance. The Go subscription's deepseek
    # models (providerID "opencode-go", modelIDs "deepseek-v4-flash" /
    # "deepseek-v4-pro") are deliberately NOT here — they stay on
    # "opencode" because the subscription, not the DeepSeek balance, pays
    # for them. Pass the account through (kimi-style): OpenCode resolves
    # the real account identity and the server's account_tag_hints flow
    # retargets it onto the operator-labeled balance-card account.
    "deepseek": ("deepseek", None),
}


def map_opencode_canonical(oc_provider_id: str) -> tuple[str, str | None] | None:
    """Return the canonical provider and optional explicit account override."""
    return _OC_CANONICAL_MAP.get((oc_provider_id or "").strip().lower())


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
            ``account_tag_hints`` payload. Used to stamp events with
            the operator's chosen account_id *after* the
            ``_OC_CANONICAL_MAP`` retag decision is known — the events
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
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, session_id, time_created, data
                FROM message
                WHERE time_created > ?
                  AND json_extract(data, '$.role') = 'assistant'
                ORDER BY time_created ASC
                """,
                (since_ms,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    for row in rows:
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
        canonical = map_opencode_canonical(oc_provider_id)
        if canonical is not None:
            canonical_provider_id, account_override = canonical
            runway_provider_id = canonical_provider_id
            if account_override is not None:
                event_account_id = account_override
                event_account_source = "tag"
            elif canonical_hints:
                provider_hints = canonical_hints.get(canonical_provider_id, {})
                # OpenCode only tells us which upstream provider served a
                # message, not which credential origin it used. Match a
                # provider-level tag directly, or use an origin tag only
                # when every reported origin agrees on one account.
                canonical_hint = provider_hints.get(f"provider:{canonical_provider_id}")
                if canonical_hint is None:
                    hinted_accounts = set(provider_hints.values())
                    if len(hinted_accounts) == 1:
                        canonical_hint = next(iter(hinted_accounts))
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

    return events
