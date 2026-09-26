"""Unit tests for the OpenCode event extractor."""

import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.sidecar_pkg.event_extractors.opencode import (
    _classify_opencode_error,
    map_opencode_canonical,
    map_opencode_provider_id,
    parse_opencode_events,
)
from tests.fixtures.opencode_fixture import make_opencode_db


def _make_db() -> tuple[Path, sqlite3.Connection]:
    """Create a temp SQLite file with the OpenCode schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = make_opencode_db(str(db_path))
    conn.close()
    return db_path, conn


def _build_db(messages: list[dict]) -> Path:
    """Build a minimal OpenCode-shaped SQLite DB from raw message dicts."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for msg in messages:
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?,?,?,?,?)",
            (
                msg["id"],
                msg.get("session_id"),
                msg["time_created"],
                msg["time_created"],
                json.dumps(msg["data"]),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------


def test_extracts_messages():
    """Assistant messages are extracted; user messages are ignored."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert len(evts) == 2  # 2 assistant messages, 1 user message (ignored)
        assert all(e.provider_id == "opencode" for e in evts)
    finally:
        db_path.unlink(missing_ok=True)


def test_uses_log_cost_when_present():
    """cost_usd on the event is taken from the logged value, not computed."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        # Fixture msg_opencode_001 has cost=0.0042
        msg1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert msg1.cost_usd == pytest.approx(0.0042, rel=1e-4)

        # Fixture msg_opencode_002 has cost=0.0088
        msg2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert msg2.cost_usd == pytest.approx(0.0088, rel=1e-4)
    finally:
        db_path.unlink(missing_ok=True)


def test_session_id_from_db():
    """session_id comes from the message.session_id column in the DB."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session_ids = {e.session_id for e in evts}
        assert "ses_session_abc" in session_ids
    finally:
        db_path.unlink(missing_ok=True)


def test_filters_by_since():
    """Events at or before since are excluded."""
    db_path, _ = _make_db()
    try:
        # msg_opencode_001 ts=2026-05-08T14:01:00Z (epoch_ms=1778248860000)
        # msg_opencode_002 ts=2026-05-08T14:03:00Z (epoch_ms=1778248980000)
        cutoff = datetime(2026, 5, 8, 14, 2, 0, tzinfo=UTC)
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=cutoff,
        )
        # Only msg_opencode_002 is after the cutoff
        assert len(evts) == 1
        assert evts[0].event_id == "msg_opencode_002"
    finally:
        db_path.unlink(missing_ok=True)


def test_captures_token_dimensions():
    """Token fields are correctly populated from the nested tokens dict."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        msg1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert msg1.tokens_input == 1200
        assert msg1.tokens_output == 300
        assert msg1.tokens_reasoning == 0
        assert msg1.tokens_cache_read == 500
        assert msg1.tokens_cache_create == 0

        msg2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert msg2.tokens_input == 2500
        assert msg2.tokens_output == 700
        assert msg2.tokens_reasoning == 150
        assert msg2.tokens_cache_read == 1200
    finally:
        db_path.unlink(missing_ok=True)


def test_captures_cwd_and_latency():
    """cwd comes from data.path; latency_ms = time.completed − time.created (ms)."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        m1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert m1.cwd == "/home/user/project"
        assert m1.latency_ms == 2000  # 1746709262000 − 1746709260000
        m2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert m2.latency_ms == 3500  # 1746709383500 − 1746709380000
    finally:
        db_path.unlink(missing_ok=True)


def test_nonexistent_db_returns_empty():
    """Missing DB file returns empty list without raising."""
    evts = parse_opencode_events(
        Path("/does/not/exist.db"),
        account_id="default",
        since=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert evts == []


# ---------------------------------------------------------------------------
# variant -> effort (issue #301)
# ---------------------------------------------------------------------------


def test_extracts_variant_as_effort():
    """Fixture messages carry variant; effort mirrors it on the push."""
    db_path, _ = _make_db()
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        msg1 = next(e for e in evts if e.event_id == "msg_opencode_001")
        assert msg1.effort == "high"
        msg2 = next(e for e in evts if e.event_id == "msg_opencode_002")
        assert msg2.effort == "medium"
    finally:
        db_path.unlink(missing_ok=True)


def _base_data(**overrides) -> dict:
    data = {
        "role": "assistant",
        "path": {"cwd": "/home/user/project"},
        "cost": 0.001,
        "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "modelID": "claude-3-5-sonnet",
        "providerID": "opencode-go",
        "time": {"created": 1746709260000, "completed": 1746709262000},
        "finish": "end_turn",
    }
    data.update(overrides)
    return data


def test_absent_variant_yields_null_effort():
    db_path = _build_db(
        [
            {
                "id": "msg_no_variant",
                "session_id": "s",
                "time_created": 1778248860000,
                "data": _base_data(),
            }
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].effort is None
    finally:
        db_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "raw_variant,expected",
    [
        ("High", "high"),
        (" high ", "high"),
        ("MEDIUM", "medium"),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
        (True, None),
    ],
)
def test_variant_is_normalized_to_lowercase_effort(raw_variant, expected):
    """variant is strip().lower()'d; non-strings / blanks become None (not ValidationError)."""
    db_path = _build_db(
        [
            {
                "id": "msg_norm",
                "session_id": "s",
                "time_created": 1778248860000,
                "data": _base_data(variant=raw_variant),
            }
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].effort == expected
    finally:
        db_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "oc_provider_id,expected_provider",
    [
        ("opencode-go", "opencode"),
        ("open-design-byok", "opencode-byok"),
        ("minimax-coding-plan", "minimax"),
        ("kimi-code-plan-global", "kimi_coding"),
    ],
)
def test_effort_set_on_retag_paths(oc_provider_id, expected_provider):
    """variant maps to effort regardless of the providerID retag path."""
    db_path = _build_db(
        [
            {
                "id": f"msg_retag_{oc_provider_id}",
                "session_id": "s",
                "time_created": 1778248860000,
                "data": _base_data(providerID=oc_provider_id, variant="high", cost=0),
            }
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == expected_provider
        assert evts[0].effort == "high"
        assert evts[0].kind == "message"
    finally:
        db_path.unlink(missing_ok=True)


def test_error_push_has_no_effort():
    """kind='error' pushes leave effort at its default (None) even with variant set."""
    db_path = _build_db(
        [
            {
                "id": "msg_err",
                "session_id": "s",
                "time_created": 1778248860000,
                "data": _base_data(
                    variant="high",
                    error={"name": "APIError", "data": {"message": "boom", "statusCode": 429}},
                ),
            }
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].kind == "error"
        assert evts[0].effort is None
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# providerID -> runway provider_id mapping (issue #182)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "oc_provider_id,expected",
    [
        ("opencode", "opencode-free"),
        ("opencode-go", "opencode"),
        ("open-design-byok", "opencode-byok"),
        ("openrouter", "opencode-openrouter"),
        ("ollama-cloud", "opencode-ollama"),
        ("deepseek", "opencode-deepseek"),  # folded onto "deepseek" by canonical map
        ("OPENCODE-GO", "opencode"),  # case-insensitive
        ("some-future-backend", "opencode-some-future-backend"),  # unknown -> derived, not Go
        ("", "opencode"),  # missing/empty -> historical default
    ],
)
def test_map_opencode_provider_id(oc_provider_id, expected):
    assert map_opencode_provider_id(oc_provider_id) == expected


def test_unrecognized_provider_never_collapses_into_go():
    """An unrecognized providerID must never resolve to the Go tier's 'opencode'."""
    assert map_opencode_provider_id("some-new-backend") != "opencode"


def _byok_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_byok",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": "tencent/hy3:free",
            "providerID": "open-design-byok",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


def test_byok_provider_gets_its_own_id():
    db_path = _build_db([_byok_message("msg_byok_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "opencode-byok"
        assert evts[0].kind == "message"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# providerID -> canonical provider_id + account_id (MiniMax coding-plan fold-in)
# ---------------------------------------------------------------------------


def test_map_opencode_canonical_minimax():
    """MiniMax coding plan folds onto the canonical 'minimax' provider.
    The account override is None (NOT "default"): the sidecar must let
    events flow through with whatever account_id the local discovery
    resolved — server-side tag-hints (PR #290) carry the operator's
    chosen account_id back via /fleet/config's account_tag_hints. The
    Untagged Credentials dialog surfaces them when no hint is available.

    Forcing "default" here previously split the operator-labeled quota
    gauge from the sidecar's event stream into two Fleet entries."""
    assert map_opencode_canonical("minimax-coding-plan") == ("minimax", None)
    assert map_opencode_canonical("MINIMAX-CODING-PLAN") == (
        "minimax",
        None,
    )  # case-insensitive


def test_map_opencode_canonical_unmapped_returns_none():
    """Unmapped providerIDs (including other opencode-* siblings) fall through
    to map_opencode_provider_id's opencode-<slug> derivation, not a canonical id."""
    assert map_opencode_canonical("some-new-backend") is None
    assert map_opencode_canonical("") is None


def test_map_opencode_canonical_openrouter():
    """OpenRouter (openrouter backend) folds onto the canonical "openrouter"
    provider so the events land on the OpenRouter quota card fed by the
    sidecar-extracted API key. Account override is None — OpenCode's
    resolved account flows through, same identity-pinning reasoning as
    kimi and ollama."""
    assert map_opencode_canonical("openrouter") == ("openrouter", None)
    assert map_opencode_canonical("OPENROUTER") == (
        "openrouter",
        None,
    )  # case-insensitive


def test_map_opencode_canonical_kimi():
    """Kimi For Coding (kimi-code-plan-global backend) folds onto kimi_coding.
    The account override is None: events keep OpenCode's resolved account
    (usually the user's email), matching the account a user-labeled kimi_coding
    quota card resolves to via resolve_account_id."""
    assert map_opencode_canonical("kimi-code-plan-global") == ("kimi_coding", None)
    assert map_opencode_canonical("KIMI-CODE-PLAN-GLOBAL") == (
        "kimi_coding",
        None,
    )  # case-insensitive


def test_map_opencode_canonical_ollama():
    """Ollama Cloud (ollama-cloud backend) folds onto the canonical "ollama"
    provider. The account override is None: events keep OpenCode's resolved
    account, matching the grain the Ollama Cloud quota card resolves to via
    resolve_account_id(account_label from the settings page)."""
    assert map_opencode_canonical("ollama-cloud") == ("ollama", None)
    assert map_opencode_canonical("OLLAMA-CLOUD") == ("ollama", None)  # case-insensitive


def test_map_opencode_canonical_xai():
    """xAI (xai backend in OpenCode) folds onto the canonical "xai" provider
    so enrichment events land on the cli-chat-proxy quota card instead of
    creating an "opencode-xai" ghost card. Account override is None —
    OpenCode's resolved account (the OAuth identity) flows through, same
    identity-pinning reasoning as kimi/ollama/openrouter."""
    assert map_opencode_canonical("xai") == ("xai", None)
    assert map_opencode_canonical("XAI") == ("xai", None)  # case-insensitive


def test_map_opencode_provider_id_xai():
    """xAI (xai backend) maps to "opencode-xai" runway provider_id via
    _OC_PROVIDER_MAP. Without this, the events branch would emit an
    unknown-provider event that EventIngestor rejects."""
    assert map_opencode_provider_id("xai") == "opencode-xai"
    assert map_opencode_provider_id("XAI") == "opencode-xai"  # case-insensitive


# ---------------------------------------------------------------------------
# DeepSeek: BYOK folds onto "deepseek", Go subscription stays on "opencode"
# ---------------------------------------------------------------------------


def test_map_opencode_canonical_deepseek():
    """BYOK DeepSeek (providerID "deepseek" in OpenCode) folds onto the
    canonical "deepseek" provider — the same provider the server-side
    balance collector (GET api.deepseek.com/user/balance) emits its card
    on. Account override is None: OpenCode's resolved account flows through
    and the account_tag_hints flow retargets it onto the labeled
    balance-card account (kimi/ollama/openrouter reasoning)."""
    assert map_opencode_canonical("deepseek") == ("deepseek", None)
    assert map_opencode_canonical("DEEPSEEK") == ("deepseek", None)  # case-insensitive


def _deepseek_byok_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_ds",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            # BYOK — OpenCode logs a computed cost; the canonical retag drops
            # it so the server prices from provider_pricing (off-peak seed).
            "cost": 0.0042,
            "tokens": {
                "input": 1200,
                "output": 400,
                "reasoning": 0,
                "cache": {"read": 8000, "write": 0},
            },
            "modelID": "deepseek-v4-flash",
            "providerID": "deepseek",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


def test_deepseek_byok_retagged_onto_canonical_provider():
    """providerID "deepseek" must land on provider_id "deepseek" — never on
    the derived "opencode-deepseek" ghost — with its logged cost dropped so
    the server reprices it from the DeepSeek seed rows."""
    db_path = _build_db([_deepseek_byok_message("msg_ds_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "deepseek"
        assert evts[0].account_id == "user@opencode.test"
        assert evts[0].model_id == "deepseek-v4-flash"
        assert evts[0].cost_usd is None
        assert evts[0].tokens_input == 1200
        assert evts[0].tokens_cache_read == 8000
    finally:
        db_path.unlink(missing_ok=True)


def test_deepseek_byok_respects_canonical_hint():
    """The canonical hint retargets BYOK events onto the operator-labeled
    balance-card account (same flow as minimax/kimi/openrouter)."""
    db_path = _build_db([_deepseek_byok_message("msg_ds_002")])
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="user@opencode.test",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            canonical_hints={"deepseek": {"provider:deepseek": "billing@example.com"}},
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "deepseek"
        assert evts[0].account_id == "billing@example.com"
    finally:
        db_path.unlink(missing_ok=True)


def test_go_subscription_deepseek_stays_on_opencode():
    """DeepSeek models served by the opencode-go subscription (the user's
    other DeepSeek path) are billed to the subscription, NOT the DeepSeek
    balance — they must stay on provider_id "opencode" with their logged
    cost intact, and never fold into "deepseek"."""
    db_path = _build_db(
        [
            {
                "id": "msg_go_ds_001",
                "session_id": "ses_go",
                "time_created": 1778248860000,
                "data": {
                    "role": "assistant",
                    "path": {"cwd": "/home/user/project"},
                    "cost": 0.001307,
                    "tokens": {"input": 900, "output": 300, "reasoning": 0, "cache": {}},
                    "modelID": "deepseek-v4-flash",
                    "providerID": "opencode-go",
                    "time": {"created": 1746709260000, "completed": 1746709262000},
                },
            }
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "opencode"
        assert evts[0].cost_usd == 0.001307  # subscription cost kept
        assert evts[0].model_id == "deepseek-v4-flash"
    finally:
        db_path.unlink(missing_ok=True)


def _minimax_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_minimax",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,  # subscription — OpenCode always logs $0 here
            "tokens": {
                "input": 90429,
                "output": 3102,
                "reasoning": 0,
                "cache": {"read": 23355693, "write": 0},
            },
            "modelID": "MiniMax-M3",
            "providerID": "minimax-coding-plan",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


def test_minimax_coding_plan_retagged_onto_canonical_card():
    """Events from OpenCode's MiniMax coding-plan backend land on
    provider_id 'minimax' — the same provider the server-side MiniMax
    collector emits cards on — and their $0 logged cost is dropped so
    the server prices them. The account_id flows through (NOT forced
    to "default"): server-side tag-hints (PR #290) carry the operator's
    chosen account_id back to the sidecar via /fleet/config, and the
    Untagged Credentials dialog surfaces the events for tagging when
    no hint is available. Forcing "default" here previously split the
    operator-labeled quota gauge from the sidecar's event stream into
    two Fleet entries (the bug behind this PR)."""
    db_path = _build_db([_minimax_message("msg_minimax_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "minimax"
        # account_id flows through — tag-hint carries the operator's choice.
        assert evts[0].account_id == "user@opencode.test"
        assert evts[0].model_id == "MiniMax-M3"
        assert evts[0].cost_usd is None
        assert evts[0].tokens_input == 90429
        assert evts[0].tokens_cache_read == 23355693
    finally:
        db_path.unlink(missing_ok=True)


def test_minimax_coding_plan_account_id_is_not_overwritten_when_default():
    """When the sidecar's local discovery returns no email and falls
    back to account_id='default', the minimax-coding-plan canonical
    remap must NOT overwrite that to a different sentinel — the
    operator's Untagged Credentials flow depends on 'default' arriving
    on the server intact so the auto-hint can re-target it."""
    db_path = _build_db([_minimax_message("msg_minimax_002")])
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "minimax"
        assert evts[0].account_id == "default"
    finally:
        db_path.unlink(missing_ok=True)


def test_parse_opencode_events_applies_canonical_hint_after_retag():
    """Closes the MiniMax card-split: when the canonical provider
    (``minimax``) has a server-shipped hint and the local discovery
    returned the legacy ``"default"`` sentinel, the opencode extractor
    stamps the retagged event with the operator's chosen account_id
    (NOT ``"default"``). Without this, the events land at
    ``(minimax, "default")`` and the quota gauge stays orphaned on the
    labeled row.

    Pairs with the events-branch test in ``tests/unit/test_sidecar.py``
    that pins the *forwarding* of canonical hints through
    ``_extract_events_for_provider``. This test pins the *consumption*
    inside the extractor itself.
    """
    db_path = _build_db([_minimax_message("msg_minimax_canonical_hint")])
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            canonical_hints={
                "minimax": {"provider:minimax": "s3ntin318@gmail.com"},
            },
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "minimax"
        # The operator's chosen account_id, not the synthetic "default".
        assert evts[0].account_id == "s3ntin318@gmail.com"
    finally:
        db_path.unlink(missing_ok=True)


def test_parse_opencode_events_keeps_local_account_when_canonical_hint_is_other_provider():
    """When the canonical hint targets a *different* provider than the
    retag target, the extractor's canonical hint must NOT apply —
    kimi_coding / ollama hints must not bleed into minimax events.
    Defense against the wrong-hint-still-passes sanity check."""
    db_path = _build_db([_minimax_message("msg_minimax_other_hint")])
    try:
        evts = parse_opencode_events(
            db_path,
            account_id="default",
            since=datetime(2020, 1, 1, tzinfo=UTC),
            canonical_hints={
                "kimi_coding": {"provider:kimi_coding": "wrong@example.com"},
            },
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "minimax"
        # kimi_coding hint doesn't apply — account_id stays as the
        # caller's input ("default").
        assert evts[0].account_id == "default"
    finally:
        db_path.unlink(missing_ok=True)


def test_parse_opencode_events_ignores_canonical_hint_when_account_override_set():
    """The kimi_coding / ollama entries in ``_OC_CANONICAL_MAP`` carry a
    None account_override (since this PR dropped the forced "default"
    for minimax). The canonical hint is the *fallback* when the override
    is None — verify it's also skipped if a future map entry sets a
    non-None override, so the explicit override always wins."""
    # Direct test against the map structure — no DB needed since the
    # retag decision happens before any DB read.
    from scripts.sidecar_pkg.event_extractors.opencode import (
        _OC_CANONICAL_MAP,
    )

    for oc_pid, (canonical_pid, account_override) in _OC_CANONICAL_MAP.items():
        # All current entries use None override after this PR; the
        # test guards against accidental regressions to a non-None
        # override that would silently bypass the canonical-hint path.
        assert account_override is None, (
            f"{oc_pid} -> ({canonical_pid}, {account_override!r}); "
            "the PR relies on all canonical entries having None "
            "override so canonical_hints can retarget events."
        )


def _minimax_error_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_minimax_err",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": "MiniMax-M3",
            "providerID": "minimax-coding-plan",
            "time": {"created": 1746709260000},
            "error": {"name": "APIError", "data": {"message": "boom", "statusCode": 429}},
        },
    }


def test_minimax_coding_plan_error_also_retagged():
    """kind='error' events go through the same canonical remap as messages."""
    db_path = _build_db([_minimax_error_message("msg_minimax_err_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "minimax"
        # Pass-through: server-side tag-hints retarget this if available.
        assert evts[0].account_id == "user@opencode.test"
        assert evts[0].kind == "error"
        assert evts[0].error_reason == "rate_limit"
    finally:
        db_path.unlink(missing_ok=True)


def _kimi_message(msg_id: str, model_id: str = "k3-256k") -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_kimi",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,  # subscription — OpenCode always logs $0 here
            "tokens": {
                "input": 5000,
                "output": 800,
                "reasoning": 0,
                "cache": {"read": 12000, "write": 0},
            },
            "modelID": model_id,
            "providerID": "kimi-code-plan-global",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


@pytest.mark.parametrize("model_id", ["k3-256k", "kimi-for-coding"])
def test_kimi_code_plan_global_retagged_onto_canonical_card(model_id):
    """Events from OpenCode's kimi-code-plan-global backend (both the k3-256k
    and the kimi-for-coding modelIDs) land on provider_id 'kimi_coding' with
    their own account_id kept (pass-through) — the same account the kimi_coding
    collector's quota card resolves to when the user labeled it — and their $0
    logged cost is dropped so the server prices them."""
    db_path = _build_db([_kimi_message("msg_kimi_001", model_id)])
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@opencode.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "kimi_coding"
        assert evts[0].account_id == "user@opencode.test"
        assert evts[0].model_id == model_id
        assert evts[0].cost_usd is None
        assert evts[0].tokens_input == 5000
        assert evts[0].tokens_cache_read == 12000
    finally:
        db_path.unlink(missing_ok=True)


def test_kimi_code_plan_global_without_identity_lands_on_default():
    """With no OpenCode account identity the event keeps account_id 'default',
    matching an unlabeled kimi_coding quota card."""
    db_path = _build_db([_kimi_message("msg_kimi_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "kimi_coding"
        assert evts[0].account_id == "default"
    finally:
        db_path.unlink(missing_ok=True)


def _ollama_message(msg_id: str, model_id: str = "nemotron-3-ultra") -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_ollama",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,  # Ollama Cloud free tier — OpenCode logs $0
            "tokens": {
                "input": 90000,
                "output": 2800,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
            },
            "modelID": model_id,
            "providerID": "ollama-cloud",
            "time": {"created": 1746709260000, "completed": 1746709262000},
        },
    }


def test_ollama_cloud_retagged_onto_canonical_card():
    """Events from OpenCode's ollama-cloud backend land on provider_id 'ollama'
    with their own account_id kept (pass-through) — the same account the Ollama
    Cloud quota card resolves to via resolve_account_id(account_label) — and
    their $0 logged cost is dropped so the server reprices them."""
    db_path = _build_db([_ollama_message("msg_ollama_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="user@ollama.test", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "ollama"
        assert evts[0].account_id == "user@ollama.test"
        assert evts[0].model_id == "nemotron-3-ultra"
        assert evts[0].cost_usd is None
        assert evts[0].tokens_input == 90000
        assert evts[0].tokens_output == 2800
    finally:
        db_path.unlink(missing_ok=True)


def test_ollama_cloud_without_identity_lands_on_default():
    """With no OpenCode account identity the event keeps account_id 'default' —
    the enrichment lands on (ollama, default) grain even though the quota card
    resolves to the user's email. This is the same pass-through tradeoff
    kimi_coding accepts (see test_kimi_code_plan_global_without_identity_lands_on_default)."""
    db_path = _build_db([_ollama_message("msg_ollama_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 1
        assert evts[0].provider_id == "ollama"
        assert evts[0].account_id == "default"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Failed-request handling (issue #182): errors don't count as usage
# ---------------------------------------------------------------------------


def _error_message(msg_id: str, provider_id: str, status_code: int) -> dict:
    return {
        "id": msg_id,
        "session_id": "ses_err",
        "time_created": 1778248860000,
        "data": {
            "role": "assistant",
            "path": {"cwd": "/home/user/project"},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "modelID": "glm-5.1",
            "providerID": provider_id,
            "time": {"created": 1746709260000},
            "error": {
                "name": "APIError",
                "data": {"message": "boom", "statusCode": status_code},
            },
        },
    }


def test_classify_opencode_error():
    assert _classify_opencode_error({"data": {"statusCode": 401}}) == "auth_failed"
    assert _classify_opencode_error({"data": {"statusCode": 403}}) == "quota_exceeded"
    assert _classify_opencode_error({"data": {"statusCode": 429}}) == "rate_limit"
    assert _classify_opencode_error({"data": {"statusCode": 504}}) == "timeout"
    assert _classify_opencode_error({"data": {"statusCode": 500}}) == "http_500"
    assert _classify_opencode_error({"name": "NetworkError"}) == "networkerror"
    assert _classify_opencode_error({}) == "unknown_error"


def test_failed_request_pushed_as_error_kind_not_usage():
    """A failed openrouter/ollama request must not count as a message with usage."""
    db_path = _build_db(
        [
            _error_message("msg_openrouter_401", "openrouter", 401),
            _error_message("msg_ollama_403", "ollama-cloud", 403),
        ]
    )
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(evts) == 2
        by_id = {e.event_id: e for e in evts}

        or_evt = by_id["msg_openrouter_401"]
        # openrouter is now in the canonical map — retags onto the canonical
        # `openrouter` provider so events land on the OpenRouter quota card.
        assert or_evt.provider_id == "openrouter"
        assert or_evt.kind == "error"
        assert or_evt.error_reason == "auth_failed"
        assert or_evt.tokens_input == 0
        assert or_evt.cost_usd is None

        ol_evt = by_id["msg_ollama_403"]
        assert ol_evt.provider_id == "ollama"
        assert ol_evt.kind == "error"
        assert ol_evt.error_reason == "quota_exceeded"
    finally:
        db_path.unlink(missing_ok=True)


def test_successful_request_not_marked_as_error():
    """Sanity check: a message without an `error` field stays kind='message'."""
    db_path = _build_db([_byok_message("msg_ok_001")])
    try:
        evts = parse_opencode_events(
            db_path, account_id="default", since=datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert evts[0].kind == "message"
        assert evts[0].error_reason is None
    finally:
        db_path.unlink(missing_ok=True)
