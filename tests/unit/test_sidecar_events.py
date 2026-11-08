"""Unit tests for sidecar event collection, attribution, tag hints, and extractors."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock

from tests.unit._sidecar_helpers import (
    _REPO_ROOT,
    isolate_host_credentials,  # noqa: F401 — fixture
)

if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import sidecar


def test_run_collection_events_use_server_hint_when_local_default(
    monkeypatch,
) -> None:
    """Local discovery returns the legacy 'default' sentinel (no real
    identity on this host). The events branch keeps iterating under
    ``"default"`` so unrelated opencode sub-providers aren't mis-
    attributed to the canonical-provider's tagged identity (PR #318
    round-2 review, Hermes warning #1). The actual card-split close
    for MiniMax happens inside ``parse_opencode_events`` via the
    ``canonical_hints`` kwarg — events that retag to minimax get
    stamped with the operator's chosen account_id at extract time,
    not at the events-branch level.

    Pins the post-PR contract:
      - ``scoped_accounts`` stays at ``["default"]`` (the branch
        does NOT apply the canonical provider's hint to the whole
        iteration).
      - The canonical hint is forwarded into the extractor as
        ``canonical_hints={"minimax": {"provider:minimax": "<email>"}}``
        so events that retag to minimax land on the labeled card.
      - The extractor test
        (``test_parse_opencode_events_applies_canonical_hint_after_retag``
        in ``tests/unit/test_event_extractor_opencode.py``) pins the
        per-event retag.
    """
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        # No matching provider account on the server for the local "default"
        # identity, but a tag-hint tells the sidecar where these events belong.
        # The server emits the hint under the canonical provider key
        # (``minimax``) because that's the provider the quota gauge lives on.
        accounts={"opencode": []},
        tokens={},
        tag_hints={
            "minimax": {"provider:minimax": "s3ntin318@gmail.com"},
        },
    )
    monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)

    # The events branch's iterating provider is opencode (not minimax);
    # local discovery returns "default" when the host has no usable
    # identity for that provider (the OpenCode DB carries no email for
    # coding-plan subscriptions).
    monkeypatch.setattr(
        sidecar,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"opencode": lambda: "default"},
    )
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"opencode"}))

    captured_account_ids: list[str] = []
    captured_canonical_hints: list[dict[str, dict[str, str]] | None] = []

    def _capture_extract(
        provider_id: str,
        account_ids: list[str],
        *,
        watermark: Any,
        bootstrap_days: int,
        out_events: list[dict[str, Any]],
        account_source=None,
        server_account_tag_hints=None,
        server_accounts_by_provider=None,
    ) -> None:
        captured_account_ids.extend(account_ids)
        # Capture the canonical_hints the events branch forwards into
        # the opencode extractor — the per-event retag depends on this.
        canonical_hints = sidecar._build_canonical_hints_for_provider(
            provider_id, server_account_tag_hints or {}
        )
        captured_canonical_hints.append(canonical_hints)
        # Emit a single synthetic event so the loop completes.
        out_events.append({"event_id": "msg_minimax_001", "kind": "message"})

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", _capture_extract)

    # No token cards → collect_provider returns nothing blocked.
    monkeypatch.setattr(
        sidecar.GenericCollector,
        "collect_provider",
        lambda *a, **kw: ([], []),
    )

    posted: dict[str, Any] = {}

    def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
        posted["entries"] = entries

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

    result = sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"},
        providers=["opencode"],
    )
    assert result.error_count == 0

    # PR #318 round-2 review (W1): events branch keeps iterating under
    # "default" — the canonical-provider hint is NOT applied to the
    # whole opencode iteration, only to events that actually retag to
    # that canonical provider (in the extractor). This avoids spreading
    # the MiniMax identity to unrelated sub-providers.
    assert captured_account_ids == ["default"]

    # The canonical_hints map IS forwarded into the opencode extractor
    # so events that retag to minimax land on the labeled quota card.
    # Opencode is the iterating provider; minimax is one of the
    # canonical providers in _OC_CANONICAL_MAP, so it must appear in
    # the forwarded map with the operator's hint under
    # ``provider:minimax``.
    assert captured_canonical_hints, "no canonical_hints forwarded to extractor"
    assert captured_canonical_hints[0].get("minimax") == {"provider:minimax": "s3ntin318@gmail.com"}

    # And the manifest gets one entry — the events branch falls through
    # to "default" and reports opencode's origin as untagged, which the
    # operator can resolve via the dialog. The per-event canonical_hints
    # forwarding is what closes the actual card-split.
    assert posted.get("entries") == [
        {
            "provider_id": "opencode",
            "credential_origin": "provider:opencode",
            "reason": "events_untagged",
        },
    ]


def test_run_collection_events_reported_untagged_when_no_hint(
    monkeypatch,
) -> None:
    """Local discovery returns nothing (or 'default') AND no server
    hint — the events still ship (so they don't disappear) but the
    provider's events-origin is reported as untagged via the same
    manifest POST the token-card branch uses. The operator can then
    tag it via the Untagged Credentials dialog to retarget future
    events onto the labeled quota card.

    Mirrors the production flow: iterating provider is ``opencode``,
    local discovery returns ``"default"``, no canonical-provider hint
    is present. The events still ship (under the legacy "default"
    sentinel) so they don't disappear, but the opencode origin is
    surfaced untagged for operator resolution.
    """
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"opencode": []},
        tokens={},
        tag_hints={},  # No hint yet — neither canonical nor iterating.
    )
    monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)

    monkeypatch.setattr(
        sidecar,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"opencode": lambda: "default"},
    )
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"opencode"}))

    captured_account_ids: list[str] = []

    def _capture_extract(
        provider_id: str,
        account_ids: list[str],
        *,
        watermark: Any,
        bootstrap_days: int,
        out_events: list[dict[str, Any]],
        account_source=None,
        server_account_tag_hints=None,
        server_accounts_by_provider=None,
    ) -> None:
        captured_account_ids.extend(account_ids)
        out_events.append({"event_id": "msg_minimax_001", "kind": "message"})

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", _capture_extract)
    monkeypatch.setattr(
        sidecar.GenericCollector,
        "collect_provider",
        lambda *a, **kw: ([], []),
    )

    posted: dict[str, Any] = {}

    def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
        posted["entries"] = entries

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

    sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"},
        providers=["opencode"],
    )

    # Events still shipped (under the legacy "default" sentinel) so
    # they don't disappear.
    assert captured_account_ids == ["default"]
    # But the origin was reported untagged so the operator can tag it.
    # The events branch reports under the iterating provider (opencode),
    # NOT the canonical provider (minimax) — see comment at the
    # events-block origin-append for why this is the right key. The
    # operator then tags ``(opencode, "provider:opencode")`` via the
    # Untagged Credentials dialog, and the opencode extractor applies
    # the canonical hint to retarget minimax-coding-plan events onto
    # the labeled quota card.
    assert posted["entries"] == [
        {
            "provider_id": "opencode",
            "credential_origin": "provider:opencode",
            "reason": "events_untagged",
        },
    ]


def _run_hermes_collection(monkeypatch, emitted: list[dict[str, Any]], tag_hints=None):
    """Run a Hermes-only ``run_collection`` whose extractor emits ``emitted``."""
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(accounts={}, tokens={}, tag_hints=tag_hints or {})
    monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)
    monkeypatch.setattr(sidecar, "_LEGACY_EVENT_ACCOUNT_DISCOVERY", {"hermes": lambda: "default"})
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"hermes"}))

    def _emit(provider_id, account_ids, *, out_events, **kw) -> int:
        out_events.extend(dict(e) for e in emitted)
        return 0

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", _emit)
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    posted: dict[str, Any] = {}

    def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
        posted["entries"] = entries
        posted["completed_providers"] = kw.get("completed_providers")

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)
    result = sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"}, providers=["hermes"]
    )
    return result, posted


def test_run_collection_hermes_resolved_events_report_no_origin(monkeypatch, caplog) -> None:
    """Hermes is not a provider: events already on a tagged account report nothing,
    and never a ``provider:hermes`` origin."""
    emitted = [
        {"event_id": "h1", "provider_id": "minimax", "account_source": "tag"},
        {"event_id": "h2", "provider_id": "kimi_coding", "account_source": "local"},
    ]
    with caplog.at_level("WARNING"):
        result, posted = _run_hermes_collection(
            monkeypatch,
            emitted,
            tag_hints={"minimax": {"provider:minimax": "me@example.com"}},
        )
    assert result.error_count == 0
    assert posted["entries"] == []
    assert posted["completed_providers"] == ["hermes"]
    assert "untagged" not in caplog.text


def test_run_collection_hermes_unresolved_events_report_their_own_provider(monkeypatch) -> None:
    """An unresolved Hermes minimax event blocks ``provider:minimax`` (taggable),
    once per provider, and a resolved event beside it adds nothing."""
    emitted = [
        {"event_id": "h1", "provider_id": "minimax", "account_source": "default"},
        {"event_id": "h2", "provider_id": "minimax", "account_source": "default"},
        {"event_id": "h3", "provider_id": "kimi_coding", "account_source": "default"},
        {"event_id": "h4", "provider_id": "opencode", "account_source": "tag"},
    ]
    _, posted = _run_hermes_collection(monkeypatch, emitted)
    assert posted["entries"] == [
        {
            "provider_id": "minimax",
            "credential_origin": "provider:minimax",
            "reason": "events_untagged",
        },
        {
            "provider_id": "kimi_coding",
            "credential_origin": "provider:kimi_coding",
            "reason": "events_untagged",
        },
    ]
    assert all(e["provider_id"] != "hermes" for e in posted["entries"])


def test_run_collection_hermes_unmapped_upstream_reports_no_origin(monkeypatch, caplog) -> None:
    """A ``hermes-<upstream>`` event (no Runway provider) has nothing to tag, so
    it ships under ``default`` without an untaggable ``provider:hermes-nous`` row."""
    emitted = [
        {"event_id": "h1", "provider_id": "hermes-nous", "account_source": "default"},
        {"event_id": "h2", "provider_id": "hermes-openai", "account_source": "default"},
        {"event_id": "h3", "provider_id": "minimax", "account_source": "default"},
    ]
    with caplog.at_level("INFO"):
        _, posted = _run_hermes_collection(monkeypatch, emitted)
    assert [e["provider_id"] for e in posted["entries"]] == ["minimax"]
    # The skip is observable, once per provider.
    assert caplog.text.count("hermes-nous have no Runway provider") == 1
    assert caplog.text.count("hermes-openai have no Runway provider") == 1


def test_run_collection_events_untagged_only_when_events_extracted(
    monkeypatch,
) -> None:
    """PR #318 round-2 review (Hermes warning #3): the events branch
    must NOT report an untagged origin when no events were extracted
    this cycle. Without this gate, a host with no Claude/Codex
    artifacts publishes a permanent ``provider:anthropic`` /
    ``provider:chatgpt`` Untagged entry for a credential it never had
    — and the operator can never resolve it via the dialog (no local
    artifact to map from).

    Pins: when the extractor returns an empty list for the provider
    (no local credential backing, no log path), the manifest entry
    must NOT be added. We exercise this with the realistic opencode
    iterating provider + ``collect_provider -> ([], [])`` to simulate
    "host has no opencode DB at all".
    """
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"opencode": []},
        tokens={},
        tag_hints={},  # No operator tag yet.
    )
    monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)

    monkeypatch.setattr(
        sidecar,
        "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
        {"opencode": lambda: "default"},
    )
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"opencode"}))

    # The extractor returns NO events — simulates a host with no opencode
    # DB at all, OR a host whose opencode DB has no events in the
    # bootstrap window. Either way: no credential evidence.
    def _capture_extract(
        provider_id: str,
        account_ids: list[str],
        *,
        watermark: Any,
        bootstrap_days: int,
        out_events: list[dict[str, Any]],
        account_source=None,
        server_account_tag_hints=None,
        server_accounts_by_provider=None,
    ) -> None:
        return  # emits nothing

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", _capture_extract)

    # No token cards.
    monkeypatch.setattr(
        sidecar.GenericCollector,
        "collect_provider",
        lambda *a, **kw: ([], []),
    )

    posted: dict[str, Any] = {}

    def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
        posted["entries"] = entries
        posted["completed_providers"] = kw.get("completed_providers")

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

    result = sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"},
        providers=["opencode"],
    )
    assert result.error_count == 0

    # PR #318 W3: when no new events were extracted, do not create a new
    # pending origin without evidence. Also withhold completion so an
    # earlier pending origin is not removed during a quiet cycle.
    assert posted.get("entries") == []
    # An empty event delta does not prove an earlier unresolved origin
    # disappeared, so this provider must not be marked complete for prune.
    assert posted.get("completed_providers") == []


def test_run_collection_events_no_untagged_when_identity_resolved(
    monkeypatch,
) -> None:
    """PR #318 round-2 re-review (Hermes W): the evidence gate must not
    fire for the *resolved* branches.

    The previous gate used the proxy
    ``scoped_accounts == [local_account_id or "default"]
    and event_hint is None`` — which was ALSO true for branches 1 and 2
    whenever ``local_account_id`` was truthy (branch 2 always stamps
    ``[local]``; branch 1 does too when local is in the server's list,
    including the ``default``/``[default]`` case). A host whose identity
    the server already knew therefore posted a phantom Untagged entry
    plus a misleading "shipping under 'default'" warning even though
    events shipped under the resolved account_id.

    Pins the explicit ``untagged`` flag: only the else-arm (no server
    match, no local identity, no hint) may report. Covers the reviewer's
    table:
      - branch 1  (local in server accounts)          → no entry
      - branch 1b (default, server has [default])     → no entry
      - branch 2  (local non-default, not in server)  → no entry
    Each case still ships its events under the resolved scoped_accounts.
    """
    from scripts.sidecar_pkg.credentials import CredentialCache

    cases = [
        # (label, server accounts, local identity, expected scoped_accounts)
        (
            "branch 1: local in server accounts",
            ["alice@example.com"],
            "alice@example.com",
            ["alice@example.com"],
        ),
        (
            "branch 1b: default local, server has default row",
            ["default"],
            "default",
            ["default"],
        ),
        (
            "branch 2: local non-default not in server accounts",
            ["alice@example.com"],
            "bob@example.com",
            ["bob@example.com"],
        ),
    ]

    for label, server_accounts, local_identity, expected_scoped in cases:
        cache = CredentialCache()
        cache.replace(
            accounts={"opencode": list(server_accounts)},
            tokens={},
            tag_hints={},  # no hint — isolation is on the branch chain
        )
        monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
        monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)
        monkeypatch.setattr(
            sidecar,
            "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
            {"opencode": (lambda li=local_identity: li)},
        )
        monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"opencode"}))

        captured_account_ids: list[str] = []

        def _capture_extract(
            provider_id: str,
            account_ids: list[str],
            *,
            watermark: Any,
            bootstrap_days: int,
            out_events: list[dict[str, Any]],
            account_source=None,
            server_account_tag_hints=None,
            server_accounts_by_provider=None,
        ) -> None:
            captured_account_ids.clear()
            captured_account_ids.extend(account_ids)
            out_events.append({"event_id": "msg_resolved_001", "kind": "message"})

        monkeypatch.setattr(sidecar, "_extract_events_for_provider", _capture_extract)
        monkeypatch.setattr(
            sidecar.GenericCollector,
            "collect_provider",
            lambda *a, **kw: ([], []),
        )

        posted: dict[str, Any] = {}

        def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
            posted["entries"] = entries

        monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

        sidecar.run_collection(
            config={"api_url": "http://x", "api_key": "k"},
            providers=["opencode"],
        )

        assert captured_account_ids == expected_scoped, f"{label}: scoped_accounts"
        # Identity resolved → no Untagged manifest entry, even though
        # events were extracted this cycle.
        assert posted.get("entries") == [], (
            f"{label}: resolved identity must not post an untagged entry, "
            f"got {posted.get('entries')!r}"
        )


def test_hermes_account_identity_defaults_and_env(monkeypatch):
    monkeypatch.delenv("HERMES_ACCOUNT_LABEL", raising=False)
    assert sidecar._hermes_account_identity() == "default"
    monkeypatch.setenv("HERMES_ACCOUNT_LABEL", "bot-team")
    assert sidecar._hermes_account_identity() == "bot-team"


def test_hermes_build_canonical_hints():
    hints = {
        "minimax": {"s3ntin3l8@gmail.com": "MiniMax Admin"},
        "unrelated": {"foo": "bar"},
    }
    extracted = sidecar._build_canonical_hints_for_provider("hermes", hints)
    assert extracted is not None
    assert "minimax" in extracted
    assert extracted["minimax"] == {"s3ntin3l8@gmail.com": "MiniMax Admin"}
    assert "unrelated" not in extracted

    assert sidecar._build_canonical_hints_for_provider("hermes", None) is None
    assert sidecar._build_canonical_hints_for_provider("unknown", hints) is None


def test_hermes_account_extractor_wiring(monkeypatch, tmp_path):
    mock_parser = MagicMock(return_value=["mock_event"])
    extractor = sidecar._make_account_extractor_hermes(mock_parser)

    class MockWatermark:
        def last_pushed(self, provider_id, account_id):
            assert provider_id == "hermes"
            assert account_id == "default"
            return

    # When no DBs exist the parser still runs, with no paths: the watermark has
    # to be compacted even on a host whose last Hermes database is gone (#926),
    # and with nothing to read it emits nothing.
    mock_parser.return_value = []
    monkeypatch.setattr(sidecar, "_discover_hermes_db_paths", lambda: [])
    assert extractor("default", MockWatermark(), 30) == []
    assert mock_parser.called
    assert mock_parser.call_args[0][0] == []

    # When DBs exist, calls parser with paths and since
    mock_parser.return_value = ["mock_event"]
    mock_parser.reset_mock()
    mock_db = tmp_path / "state.db"
    mock_db.touch()
    monkeypatch.setattr(sidecar, "_discover_hermes_db_paths", lambda: [mock_db])
    events = extractor("default", MockWatermark(), 30, canonical_hints={"minimax": {}})
    assert events == ["mock_event"]
    assert mock_parser.called
    args, kwargs = mock_parser.call_args
    assert args[0] == [mock_db]
    assert kwargs["account_id"] == "default"
    assert kwargs["canonical_hints"] == {"minimax": {}}
    assert kwargs["since"] is not None
    assert kwargs["state_file"] is None


def test_hermes_account_extractor_dedup_and_state_file(monkeypatch, tmp_path):
    class FakeEvent:
        def __init__(self, event_id):
            self.event_id = event_id

    mock_parser = MagicMock(return_value=[FakeEvent("e1"), FakeEvent("e2"), FakeEvent("e1")])
    custom_state_file = tmp_path / "custom_watermark.json"
    extractor = sidecar._make_account_extractor_hermes(mock_parser, state_file=custom_state_file)

    class MockWatermark:
        def last_pushed(self, provider_id, account_id):
            return None

    mock_db = tmp_path / "state.db"
    mock_db.touch()
    monkeypatch.setattr(sidecar, "_discover_hermes_db_paths", lambda: [mock_db])

    events = extractor("default", MockWatermark(), 30)
    assert [e.event_id for e in events] == ["e1", "e2"]
    args, kwargs = mock_parser.call_args
    assert kwargs["state_file"] == custom_state_file


class TestMiniMaxCodeSchedulingId:
    """The MiniMax Code runtime ledger is collected under its own scheduling id.

    The ledger lives in a local SQLite store on every host that ran the
    client, while the ``minimax`` *provider* only exists once the host has
    a MiniMax account (a provider config row or a quota card). Sharing the
    provider's id tied the ledger's collection to that account, so a host
    with neither saw its usage only on full cycles (#928).
    """

    @staticmethod
    def _collection(
        monkeypatch, tmp_path, *, providers, tag_hints=None, parser=None, accounts=None
    ):
        """Run a ``run_collection`` with the MiniMax Code extractor stubbed.

        Everything else — the account resolution, the untagged origin and
        the event stamping — is the real code path.
        """
        from app.models.schemas import UsageEventPush
        from scripts.sidecar_pkg.credentials import CredentialCache
        from scripts.sidecar_pkg.event_extractors import minimax_code as mm

        cache = CredentialCache()
        cache.replace(accounts=accounts or {}, tokens={}, tag_hints=tag_hints or {})
        monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
        monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)
        monkeypatch.setattr(
            sidecar,
            "_LEGACY_EVENT_ACCOUNT_DISCOVERY",
            {"minimax": lambda: "default", "minimax_code": lambda: "default"},
        )
        monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset({"minimax", "minimax_code"}))
        monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
        posted: dict[str, Any] = {}

        def _capture_manifest(*, entries, **kw):
            posted["entries"] = entries

        monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

        store = tmp_path / "runtime-state.sqlite"
        store.touch()
        monkeypatch.setattr(sidecar, "_discover_minimax_code_db_paths", lambda: [store])

        parsed: list[dict[str, Any]] = []

        def _parse(db_path, *, account_id, since, state_file):
            parsed.append({"path": str(db_path), "account_id": account_id})
            return [
                UsageEventPush(
                    provider_id="minimax",
                    account_id=account_id,
                    event_id="mm-1",
                    ts="2026-10-20T10:00:00Z",
                    source_app="minimax_code",
                )
            ]

        monkeypatch.setattr(mm, "parse_minimax_code_events", parser or _parse)
        result = sidecar.run_collection(
            config={"api_url": "http://x", "api_key": "k"},
            providers=providers,
        )
        return result, posted, parsed

    def test_the_events_keep_the_minimax_provider_id(self, monkeypatch, tmp_path):
        """The scheduling id is the loop's key; the events still belong to
        the ``minimax`` provider, so the server files them under it."""
        result, _, _ = self._collection(monkeypatch, tmp_path, providers=["minimax_code"])
        assert result.error_count == 0
        assert [e["provider_id"] for e in result.events] == ["minimax"]
        assert [e["source_app"] for e in result.events] == ["minimax_code"]

    def test_the_minimax_account_hint_is_applied(self, monkeypatch, tmp_path):
        """The hint lives in the *provider's* bucket, not the scheduling id's.

        Reading it from the scheduling id would miss ``provider:minimax``
        and drop every event back to "default" — the very "Unassigned
        usage" state the new id exists to fix.
        """
        result, _, _ = self._collection(
            monkeypatch,
            tmp_path,
            providers=["minimax_code"],
            tag_hints={"minimax": {"provider:minimax": "me@example.com"}},
        )
        assert [e["account_id"] for e in result.events] == ["me@example.com"]

    def test_the_servers_account_list_is_read_under_the_provider(self, monkeypatch, tmp_path):
        """The server's accounts are the provider's, not the scheduling id's.

        When the server knows a ``minimax`` account, the MiniMax Code
        iteration must resolve against that list — and land on the same
        branch the ``minimax`` iteration lands on, manifest entry and all.
        """
        result, posted, _ = self._collection(
            monkeypatch,
            tmp_path,
            providers=["minimax_code"],
            accounts={"minimax": ["default"]},
        )
        assert [e["account_source"] for e in result.events] == ["local"]
        # Resolved locally: no untagged origin to report.
        assert posted["entries"] == []

    def test_an_untagged_origin_is_the_provider_s_one(self, monkeypatch, tmp_path):
        """Without a hint the events ship under "default" and the operator is
        pointed at ``provider:minimax`` — the origin they can actually tag.

        ``provider:minimax_code`` would be a provider that does not exist,
        and an Untagged entry the operator can never clear.
        """
        _, posted, _ = self._collection(monkeypatch, tmp_path, providers=["minimax_code"])
        assert posted["entries"] == [
            {
                "provider_id": "minimax",
                "credential_origin": "provider:minimax",
                "reason": "events_untagged",
            }
        ]

    def test_the_ledger_is_read_once_per_cycle(self, monkeypatch, tmp_path):
        """Both scheduling ids dispatch the same extractor.

        A host with a MiniMax account is scheduled for both ``minimax`` and
        ``minimax_code``, so without a per-cycle guard the same rows would
        be read — and every event pushed — twice per cycle.
        """
        result, _, parsed = self._collection(
            monkeypatch, tmp_path, providers=["minimax", "minimax_code"]
        )
        assert len(parsed) == 1
        assert len(result.events) == 1

    def test_a_failing_store_is_still_tried_by_the_other_id(self, monkeypatch, tmp_path):
        """A store whose parse raises must stay open for the other id.

        Marking it read before the parse would skip it for the rest of the
        cycle: no retry, no error count on the second attempt, and the
        provider would still report the cycle complete.
        """
        from scripts.sidecar_pkg.event_extractors import minimax_code as mm

        attempts: list[str] = []

        def _raise(db_path, *, account_id, since, state_file):
            attempts.append(str(db_path))
            raise RuntimeError("boom")

        monkeypatch.setattr(mm, "parse_minimax_code_events", _raise)
        result, _, _ = self._collection(
            monkeypatch,
            tmp_path,
            providers=["minimax", "minimax_code"],
            parser=_raise,
        )
        assert len(attempts) == 2
        assert result.error_count == 2

    def test_a_server_that_knows_only_the_provider_still_collects(self, monkeypatch, tmp_path):
        """An older server sends ``minimax`` but not ``minimax_code``.

        The ledger must still be collected there, or a sidecar newer than
        its server would silently stop reporting MiniMax Code usage.
        """
        result, _, parsed = self._collection(monkeypatch, tmp_path, providers=["minimax"])
        assert len(parsed) == 1
        assert [e["provider_id"] for e in result.events] == ["minimax"]


def test_the_hermes_extractor_compacts_the_watermark_without_any_database(monkeypatch, tmp_path):
    """A host whose last Hermes database is gone still has to compact.

    Finding no database is not "nothing to do": the watermark is full of the
    keys of databases that no longer exist, and the parser is what prunes
    them (#926). An early return would keep every key forever.
    """
    from scripts.sidecar_pkg.event_extractors.hermes import (
        _load_hermes_watermark,
        parse_hermes_events,
    )
    from scripts.sidecar_pkg.event_watermark import EventWatermark

    state_file = tmp_path / "hermes_watermark.jsonl"
    gone_db = tmp_path / "profiles" / "gone" / "state.db"
    state_file.write_text(
        json.dumps(
            {
                "db": str(gone_db.resolve()),
                "accounts": {
                    "default": {
                        "default|sess|model|hermes|||": {
                            "input_tokens": 1,
                            "output_tokens": 0,
                            "cost_usd": 0.0,
                            "api_call_count": 1,
                            "emission_seq": 1,
                            "last_seen": 1.0,
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sidecar, "_discover_hermes_db_paths", lambda: [])

    extractor = sidecar._make_account_extractor_hermes(parse_hermes_events, state_file=state_file)
    events = extractor("default", EventWatermark(tmp_path / "event-watermark.json"), 30)

    assert events == []
    # The log is append-only: the slice is superseded by a null, so the
    # state it holds is empty even though the line is still in the file.
    state, _ = _load_hermes_watermark(state_file)
    assert not any(slices for accounts in state.values() for slices in accounts.values())
