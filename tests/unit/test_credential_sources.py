from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import (
    CredentialSource,
    CredentialTag,
    LatestUsage,
    ProviderAccountLabel,
    ProviderConfig,
    UsageEvent,
)
from app.services.credential_sources import (
    HEALTH_DETAILS,
    account_sources,
    configured_account_ids,
    describe_origin,
    describe_origin_full,
    effective_health,
    login_hint,
    merge_source_provenance,
    phantom_accounts,
    prune_server_sources,
    real_account_ids,
    record_source_result,
    register_server_source,
    reset_source_retry,
    resolve_source_account,
    sidecar_source_id,
    touch_source,
)


def test_describe_origin_returns_safe_type_and_label():
    assert describe_origin("env:OPENROUTER_API_KEY") == ("env", "OPENROUTER_API_KEY")
    assert describe_origin("path:/home/alice/.config/auth.json#fingerprint") == (
        "file",
        "auth.json",
    )
    assert describe_origin("cookie:browser") == ("cookie", "Browser cookie")
    assert describe_origin("keychain:Claude Code-credentials") == ("keychain", "Keychain entry")
    assert describe_origin(None) == ("sidecar", "Sidecar credential")


def test_sidecar_source_id_is_host_scoped_and_stable():
    source_id = sidecar_source_id("host-a", "path:~/.config/auth.json")
    assert source_id == sidecar_source_id("host-a", "path:~/.config/auth.json")
    assert source_id != sidecar_source_id("host-b", "path:~/.config/auth.json")


def test_touch_source_refresh_preserves_preferences_and_health():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sidecar = touch_source(
            session,
            provider_id="openrouter",
            account_id="Alice@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
            credential_origin="path:auth.json",
            sidecar_id="host-a",
        )
        session.commit()
        sidecar.enabled = False
        sidecar.priority = 5
        sidecar.health = "unavailable"
        sidecar.health_detail = "Missing from last scan"
        session.add(sidecar)
        session.commit()

        refreshed = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="host-a",
            source_type="env",
            source_label="OPENROUTER_API_KEY",
            credential_origin="env:OPENROUTER_API_KEY",
            sidecar_id="host-a",
        )
        session.commit()

        assert refreshed.enabled is False
        assert refreshed.priority == 5
        assert refreshed.health == "unavailable"
        assert refreshed.health_detail == "Missing from last scan"
        assert refreshed.source_label == "OPENROUTER_API_KEY"


def test_config_source_is_inserted_first_and_shifts_existing_priority():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        sidecar = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
        )
        sidecar.priority = 5
        session.add(sidecar)
        config = touch_source(
            session,
            provider_id="openrouter",
            account_id="alice@example.com",
            source_id="config:openrouter:alice@example.com",
            source_type="config",
            source_label="Manual configuration",
        )
        session.commit()
        assert config.priority == 0
        assert sidecar.priority == 6
        assert account_sources(session, "openrouter", "ALICE@example.com") == [
            config,
            sidecar,
        ]


def _mem_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_touch_source_without_metadata_preserves_expiry_and_token_types():
    """Ingest knows the secrets but not the health metadata the manifest reported;
    its refresh must not wipe the expiry/token types the manifest recorded."""
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    common = {
        "provider_id": "gemini",
        "account_id": "alice@example.com",
        "source_id": "sidecar:host-a:oauth",
        "source_type": "file",
        "source_label": "oauth_creds.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        touch_source(session, **common, credential_expires_at=expires, token_types=["oauth_token"])
        row = touch_source(session, **common)  # ingest-style: no metadata
        assert row.token_types_json == '["oauth_token"]'
        assert row.credential_expires_at is not None
        assert row.credential_expires_at.replace(tzinfo=UTC) == expires


def test_touch_source_explicit_none_clears_expiry():
    """``None`` is a real value ("this credential has no expiry"), unlike omission."""
    common = {
        "provider_id": "gemini",
        "account_id": "alice@example.com",
        "source_id": "sidecar:host-a:oauth",
        "source_type": "file",
        "source_label": "oauth_creds.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        touch_source(
            session,
            **common,
            credential_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            token_types=["oauth_token"],
        )
        row = touch_source(session, **common, credential_expires_at=None, token_types=[])
        assert row.credential_expires_at is None
        assert row.token_types_json == "[]"


def test_resolve_source_account_prefers_real_identity_over_default():
    common = {
        "provider_id": "chatgpt",
        "source_id": "sidecar:host-a:auth",
        "source_type": "file",
        "source_label": "auth.json",
        "sidecar_id": "host-a",
    }
    with _mem_session() as session:
        assert resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") is None
        touch_source(session, **common, account_id="default")
        assert resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") == "default"
        touch_source(session, **common, account_id="alice@example.com")
        assert (
            resolve_source_account(session, "chatgpt", "sidecar:host-a:auth") == "alice@example.com"
        )
        assert resolve_source_account(session, "gemini", "sidecar:host-a:auth") is None


def _source_row(**overrides) -> CredentialSource:
    return CredentialSource(
        **{
            "provider_id": "openrouter",
            "account_id": "alice@example.com",
            "source_id": "host-a",
            "source_type": "file",
            "source_label": "auth.json",
            **overrides,
        }
    )


def test_record_source_result_stamps_provenance_for_each_outcome():
    row = _source_row()

    record_source_result(row, "healthy")
    assert (row.health, row.health_detail, row.last_error) == ("healthy", None, None)
    assert row.last_attempt_at is not None and row.last_success_at is not None
    first_success = row.last_success_at

    # A failure records the attempt and error but keeps the last success.
    record_source_result(row, "auth_failed")
    assert row.health == "auth_failed"
    assert row.last_error == "Authentication failed"
    assert row.last_success_at == first_success

    record_source_result(row, "unavailable")
    assert (row.health_detail, row.last_error) == ("Collection failed", "Collection failed")

    # Degraded still produced quota data: it counts as a success, with a note.
    record_source_result(row, "degraded")
    assert row.last_success_at is not None and row.last_success_at >= first_success
    assert row.last_error == "Some requests were rejected; quota was collected"


def test_register_server_source_is_idempotent_and_has_no_origin():
    with _mem_session() as session:
        first = register_server_source(
            session,
            provider_id="github",
            account_id="default",
            source_type="env",
            label="GITHUB_TOKEN",
        )
        again = register_server_source(
            session,
            provider_id="github",
            account_id="default",
            source_type="env",
            label="GITHUB_TOKEN",
        )
        assert first.id == again.id
        assert first.source_id == "server:github:env:GITHUB_TOKEN"
        # Origins are what operator tags and sidecar moves match on; a server env
        # var is not a sidecar origin.
        assert first.credential_origin is None and first.sidecar_id is None


def test_register_server_source_follows_identity_resolved_later():
    """First seen before its identity resolved (``default``), then under the real
    account: the source must move, not duplicate."""
    with _mem_session() as session:
        for account in ("default", "s3ntin3l8"):
            register_server_source(
                session,
                provider_id="github",
                account_id=account,
                source_type="env",
                label="GITHUB_TOKEN",
            )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["s3ntin3l8"]


def test_register_server_source_drops_placeholder_when_real_row_exists():
    with _mem_session() as session:
        register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        # Legacy placeholder left behind for the same source.
        session.add(
            _source_row(
                provider_id="github",
                account_id="default",
                source_id="server:github:env:T",
                source_type="env",
                source_label="T",
            )
        )
        session.commit()
        register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["s3ntin3l8"]


def test_register_server_source_drops_the_row_left_under_a_previous_account():
    """A rotated token resolving to a different login must not leave a duplicate."""
    with _mem_session() as session:
        for account in ("alice", "bob"):
            register_server_source(
                session, provider_id="github", account_id=account, source_type="env", label="T"
            )
        rows = session.exec(select(CredentialSource)).all()
        assert [r.account_id for r in rows] == ["bob"]


def test_prune_server_sources_removes_only_vanished_server_rows():
    with _mem_session() as session:
        for label in ("KEEP", "GONE"):
            register_server_source(
                session, provider_id="github", account_id="a", source_type="env", label=label
            )
        register_server_source(
            session, provider_id="openrouter", account_id="a", source_type="env", label="GONE"
        )
        touch_source(
            session,
            provider_id="github",
            account_id="a",
            source_id="sidecar:x",
            source_type="file",
            source_label="auth.json",
            sidecar_id="host-a",
        )
        removed = prune_server_sources(session, "github", {"server:github:env:KEEP"})
        left = {(r.provider_id, r.source_id) for r in session.exec(select(CredentialSource)).all()}

    assert removed == 1
    # Other providers' server rows and machine-reported rows are never touched.
    assert left == {
        ("github", "server:github:env:KEEP"),
        ("openrouter", "server:openrouter:env:GONE"),
        ("github", "sidecar:x"),
    }


def test_default_keyed_registration_adopts_the_resolved_row_and_keeps_its_provenance():
    """An unresolved collection (identity not obtained this cycle) must not displace the
    row a resolved one registered — that lost last_success_at and flipped the row between
    accounts while identity resolution flapped."""
    with _mem_session() as session:
        resolved = register_server_source(
            session, provider_id="github", account_id="s3ntin3l8", source_type="env", label="T"
        )
        record_source_result(resolved, "healthy")
        session.commit()
        success = resolved.last_success_at
        assert success is not None

        again = register_server_source(
            session, provider_id="github", account_id="default", source_type="env", label="T"
        )
        session.commit()

        rows = session.exec(select(CredentialSource)).all()
        assert [(r.account_id, r.id) for r in rows] == [("s3ntin3l8", resolved.id)]
        assert again.id == resolved.id and again.last_success_at == success


def test_default_keyed_registration_stays_default_when_nothing_is_resolved_yet():
    with _mem_session() as session:
        row = register_server_source(
            session, provider_id="github", account_id="default", source_type="env", label="T"
        )
        assert row.account_id == "default"


def test_configured_account_ids_only_reports_provider_configs():
    with _mem_session() as session:
        session.add(ProviderConfig(provider_id="openrouter", account_id="alice@example.com"))
        session.commit()
        touch_source(
            session,
            provider_id="openrouter",
            account_id="bob@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
            sidecar_id="host-a",
        )

        assert configured_account_ids(session, "openrouter") == {"alice@example.com"}
        assert configured_account_ids(session, "minimax") == set()


def test_real_account_ids_union_every_kind_of_evidence():
    with _mem_session() as session:
        session.add(ProviderConfig(provider_id="openrouter", account_id="alice@example.com"))
        session.add(
            LatestUsage(
                provider_id="openrouter",
                account_id="bob@example.com",
                window_type="daily",
                variant="",
                model_id="",
                card_json="{}",
            )
        )
        session.add(
            UsageEvent(
                provider_id="openrouter",
                account_id="carol@example.com",
                sidecar_id="dev-01",
                event_id="msg_1",
                ts=datetime(2026, 9, 1, tzinfo=UTC),
                kind="message",
                model_id="gpt-5",
                tokens_input=10,
                tokens_output=5,
                cost_usd=0.01,
                attribution_source="default",
            )
        )
        session.add(
            CredentialTag(
                provider_id="openrouter",
                credential_origin="env:OPENROUTER_API_KEY",
                account_id="dave@example.com",
            )
        )
        session.add(
            ProviderAccountLabel(
                provider_id="openrouter",
                account_id="erin@example.com",
                account_label="Erin",
            )
        )
        session.commit()

        assert real_account_ids(session, "openrouter") == {
            "alice@example.com",
            "bob@example.com",
            "carol@example.com",
            "dave@example.com",
            "erin@example.com",
        }
        assert real_account_ids(session, "minimax") == set()


def test_phantom_accounts_are_the_ones_with_only_credential_rows():
    """The account an account rename left behind: its credential rows survive
    with no config, card or events of their own."""
    with _mem_session() as session:
        touch_source(
            session,
            provider_id="deepseek",
            account_id="bd6d58cf",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
            sidecar_id="host-a",
        )
        touch_source(
            session,
            provider_id="deepseek",
            account_id="alice@example.com",
            source_id="host-a",
            source_type="file",
            source_label="auth.json",
            sidecar_id="host-a",
        )
        session.add(ProviderConfig(provider_id="deepseek", account_id="alice@example.com"))
        session.commit()

        assert phantom_accounts(session, "deepseek") == {"bd6d58cf"}
        # no source rows for this provider at all: nothing to be phantom about
        assert phantom_accounts(session, "minimax") == set()


def test_a_tag_or_label_keeps_a_configless_account_off_the_phantom_list():
    """An operator can point a credential at a discovered account that has no
    configuration, card or usage yet. That is a deliberate choice, so ingest
    and the repair must not read it as a rename leftover."""
    with _mem_session() as session:
        for account_id, source_id in (
            ("discovered@example.com", "host-a"),
            ("labelled@example.com", "host-b"),
            ("bd6d58cf", "host-c"),
        ):
            touch_source(
                session,
                provider_id="deepseek",
                account_id=account_id,
                source_id=source_id,
                source_type="file",
                source_label="auth.json",
                sidecar_id=source_id,
            )
        session.add(
            CredentialTag(
                provider_id="deepseek",
                credential_origin="path:/auth.json",
                account_id="discovered@example.com",
                sidecar_id="host-a",
            )
        )
        session.add(
            ProviderAccountLabel(
                provider_id="deepseek",
                account_id="labelled@example.com",
                account_label="Labelled",
            )
        )
        session.commit()

        assert phantom_accounts(session, "deepseek") == {"bd6d58cf"}


def test_effective_health_only_trusts_a_recorded_attempt():
    row = _source_row()
    assert row.health == "healthy" and row.last_attempt_at is None
    assert effective_health(row) == "untried"  # registered, never collected

    record_source_result(row, "healthy")
    assert effective_health(row) == "healthy"

    # A legacy row with a real non-default health keeps it even without an attempt time.
    legacy = _source_row(health="auth_failed")
    assert legacy.last_attempt_at is None
    assert effective_health(legacy) == "auth_failed"


def test_effective_health_counts_a_legacy_success_as_an_attempt():
    row = _source_row(last_success_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert row.last_attempt_at is None
    assert effective_health(row) == "healthy"


def _attempted(when, health, **extra):
    return _source_row(
        health=health,
        health_detail=HEALTH_DETAILS.get(health),
        last_attempt_at=when,
        last_error=HEALTH_DETAILS.get(health),
        **extra,
    )


def test_merge_source_provenance_takes_the_more_recent_attempt():
    early, late = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)

    target = _attempted(early, "auth_failed", consecutive_failures=4, next_retry_at=late)
    merge_source_provenance(target, _attempted(late, "healthy", last_success_at=late))
    assert (target.health, target.last_attempt_at, target.last_success_at) == (
        "healthy",
        late,
        late,
    )
    # The rest period belongs to the failure it was earned by, not to the merged row.
    assert (target.consecutive_failures, target.next_retry_at) == (0, None)

    # A newer target keeps its own history; an older source never overwrites it.
    target = _attempted(late, "auth_failed")
    merge_source_provenance(target, _attempted(early, "healthy", last_success_at=early))
    assert (target.health, target.last_attempt_at, target.last_success_at) == (
        "auth_failed",
        late,
        None,
    )

    # Equal timestamps: the target stays.
    target = _attempted(late, "auth_failed")
    merge_source_provenance(target, _attempted(late, "healthy"))
    assert target.health == "auth_failed"

    # A source that was never attempted adds nothing to an attempted target.
    target = _attempted(late, "healthy", last_success_at=late)
    merge_source_provenance(target, _source_row(health="auth_failed"))
    assert (target.health, target.last_attempt_at) == ("healthy", late)


def test_merge_source_provenance_carries_a_legacy_row_whole():
    target = _source_row()
    legacy = _source_row(
        health="auth_failed",
        health_detail="Authentication failed",
        last_error="Authentication failed",
    )
    merge_source_provenance(target, legacy)
    assert (target.health, target.health_detail, target.last_error) == (
        "auth_failed",
        "Authentication failed",
        "Authentication failed",
    )


def test_rejected_source_backs_off_on_the_shared_schedule_and_a_success_clears_it():
    row = _source_row()
    record_source_result(row, "auth_failed")
    assert row.consecutive_failures == 1
    first = row.next_retry_at
    assert first is not None
    assert timedelta(minutes=14) < first - datetime.now(UTC) <= timedelta(minutes=15)

    record_source_result(row, "auth_failed")
    assert row.consecutive_failures == 2
    assert timedelta(minutes=29) < row.next_retry_at - datetime.now(UTC) <= timedelta(minutes=30)

    record_source_result(row, "healthy")
    assert (row.consecutive_failures, row.next_retry_at) == (0, None)


def test_failure_streak_start_is_stamped_once_and_cleared_by_a_success():
    row = _source_row()
    record_source_result(row, "unavailable")
    started = row.failing_since
    assert started is not None
    record_source_result(row, "unavailable")
    record_source_result(row, "auth_failed")
    assert row.failing_since == started  # the streak began at the first failure
    record_source_result(row, "healthy")
    assert row.failing_since is None


def test_a_non_auth_failure_ends_the_rest_an_earlier_rejection_earned():
    row = _source_row()
    record_source_result(row, "auth_failed")
    assert row.next_retry_at is not None
    record_source_result(row, "unavailable")
    assert row.next_retry_at is None


def test_unavailable_counts_failures_but_never_rests_the_source():
    row = _source_row()
    for expected in (1, 2, 3):
        record_source_result(row, "unavailable")
        assert row.consecutive_failures == expected
        assert row.next_retry_at is None


def test_reset_source_retry_clears_only_that_source():
    with _mem_session() as session:
        for source_id in ("src:a", "src:b"):
            row = touch_source(
                session,
                provider_id="openrouter",
                account_id="alice@example.com",
                source_id=source_id,
                source_type="file",
                source_label="auth.json",
                credential_origin="path:auth.json",
                sidecar_id="host-a",
            )
            record_source_result(row, "auth_failed")
        session.commit()

        reset_source_retry(session, provider_id="openrouter", source_id="src:a")
        session.commit()

        rows = {r.source_id: r for r in session.exec(select(CredentialSource))}
        assert (rows["src:a"].consecutive_failures, rows["src:a"].next_retry_at) == (0, None)
        assert rows["src:b"].consecutive_failures == 1 and rows["src:b"].next_retry_at is not None


@pytest.mark.parametrize(
    ("origin", "app", "label", "path"),
    [
        ("path:/home/bjoern/.codex/auth.json#fp", "Codex CLI", "auth.json", "~/.codex/auth.json"),
        (
            "path:/home/bjoern/.local/share/opencode/auth.json",
            "OpenCode",
            "auth.json",
            "~/.local/share/opencode/auth.json",
        ),
        (
            r"path:C:\Users\bjoer\.codex\auth.json",
            "Codex CLI",
            "auth.json",
            r"~\.codex\auth.json",
        ),
        (
            r"path:C:\Users\bjoer\AppData\Roaming\codex\auth.json",
            "Codex CLI",
            "auth.json",
            r"~\AppData\Roaming\codex\auth.json",
        ),
        (
            "path:/Users/b/Library/Application Support/claude/.credentials.json",
            "Claude Code",
            ".credentials.json",
            "~/Library/Application Support/claude/.credentials.json",
        ),
        ("path:/srv/other/auth.json", None, "auth.json", "/srv/other/auth.json"),
        ("file:///home/bob/.codex/auth.json", "Codex CLI", "auth.json", "~/.codex/auth.json"),
        ("path:/mnt/c/Users/bob/.codex/auth.json", "Codex CLI", "auth.json", "~/.codex/auth.json"),
        ("path:/var/home/bob/.codex/auth.json", "Codex CLI", "auth.json", "~/.codex/auth.json"),
        ("path:/home/kimi-code/x/y.json", None, "y.json", "~/x/y.json"),
        ("path:/data/state/quota.json", None, "quota.json", "/data/state/quota.json"),
    ],
)
def test_describe_origin_full_names_the_owning_app(origin, app, label, path):
    display = describe_origin_full(origin)
    assert (display.kind, display.label, display.app, display.path) == ("file", label, app, path)


def test_describe_origin_full_non_file_origins():
    assert describe_origin_full("env:GITHUB_TOKEN").app is None
    assert describe_origin_full("cookie:chatgpt/session").app == "Browser"
    assert describe_origin_full("keychain:Claude Code-credentials").app == "Claude Code"
    assert describe_origin_full("keychain:Claude Code-credentials").path is None


def test_login_hint_known_and_unknown():
    assert login_hint("Codex CLI") == "run `codex login`"
    assert login_hint("Claude Code") == "run `claude`, then `/login`"  # no shell subcommand
    assert login_hint("Gemini CLI") == "run `gemini`, then `/auth`"
    assert login_hint("Browser") is None
    assert login_hint(None) is None


_HOMES = {
    "linux": ("/home/u", "/home/u/.config", "/home/u/.local/share"),
    "macos": (
        "/Users/u",
        "/Users/u/Library/Application Support",
        "/Users/u/Library/Application Support",
    ),
    "windows": (r"C:\Users\u", r"C:\Users\u\AppData\Roaming", r"C:\Users\u\AppData\Roaming"),
}


def _registry_file_paths() -> list[str]:
    root = Path(__file__).resolve().parents[2]
    registry_json = json.loads((root / "app/core/registry.json").read_text())
    overlay = json.loads((root / "scripts/sidecar_registry_overlay.json").read_text())
    rules = [r for p in registry_json["providers"].values() for r in p.get("rules", [])]
    rules += [add["rule"] for p in overlay["providers"].values() for add in p.get("add_rules", [])]
    paths: list[str] = []
    for rule in rules:
        if rule.get("type") in ("file", "xai_grok_cli_auth") or str(
            rule.get("type", "")
        ).startswith("file_json"):
            # An overlay rule that the sidecar doesn't implement is dropped, so skip dropped types.
            paths.extend(rule.get("paths", []))
    return paths


@pytest.mark.parametrize("platform", sorted(_HOMES))
def test_every_registry_file_path_names_an_app_on_every_platform(platform):
    home, config, data = _HOMES[platform]
    sep = "\\" if platform == "windows" else "/"
    unmatched = []
    for raw in _registry_file_paths():
        if raw.endswith("quota.json") and "antigravity" not in raw:
            continue
        expanded = re.sub(r"\{\{CONFIG_DIR:([^}]+)\}\}", lambda m: config + sep + m[1], raw)
        expanded = re.sub(r"\{\{DATA_DIR:([^}]+)\}\}", lambda m: data + sep + m[1], expanded)
        expanded = expanded.replace("~", home, 1) if expanded.startswith("~") else expanded
        if sep == "\\":
            expanded = expanded.replace("/", "\\")
        display = describe_origin_full(f"path:{expanded}")
        if display.app is None or display.path is None or not display.path.startswith("~"):
            unmatched.append((raw, expanded, display))
    assert not unmatched
