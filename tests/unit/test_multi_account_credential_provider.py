"""Multi-account hardening tests for `app.services.credential_provider`.

The hotspot the multi-account work fixes is the silent `.first()` collapse
across multiple `(provider_id, account_id)` `ProviderConfig` rows. After
hardening, callers must be able to:

- Pass an explicit `account_id` to fetch a specific account's credentials.
- Omit `account_id` when exactly one row exists (`account_id="default"` is
  the canonical today-state, but any single row is acceptable).
- Get an unambiguous error when multiple rows exist and no `account_id`
  was supplied — never silently pick an arbitrary row.

Tests exercise the helper directly + the public API surface.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.credential_provider import (
    AmbiguousProviderAccountError,
    CredentialProvider,
    _resolve_provider_config,
)


def _row(provider_id: str, account_id: str, *, enabled: bool = True) -> object:
    """Tiny stand-in for a `ProviderConfig` ORM row.

    The helper only reads `provider_id`, `account_id`, and `enabled`; the
    public methods read `api_key` and `session_cookie` as well.
    """

    class _Row:
        pass

    r = _Row()
    r.provider_id = provider_id
    r.account_id = account_id
    r.enabled = enabled
    r.api_key = None
    r.session_cookie = None
    return r


class _FakeSession:
    """Replaces `sqlmodel.Session(engine)` for these unit tests.

    Mimics the slice of the SQLModel API the helper uses: `exec(stmt).all()`
    and `exec(stmt).first()`.
    """

    def __init__(self, *, rows_by_provider: dict[str, list[object]]):
        self._rows_by_provider = rows_by_provider

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def exec(self, stmt):  # noqa: ANN001 - signature dictated by SQLModel
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        where_str = str(compiled)

        pid_filter = None
        aid_filter = None
        enabled_filter = False
        if "provider_configs.provider_id" in where_str:
            for prov_id in self._rows_by_provider:
                if f"'{prov_id}'" in where_str:
                    pid_filter = prov_id
                    break
        if "provider_configs.account_id" in where_str:
            for rows in self._rows_by_provider.values():
                for r in rows:
                    if f"'{r.account_id}'" in where_str:
                        aid_filter = r.account_id
                        break
                if aid_filter is not None:
                    break
        if "provider_configs.enabled" in where_str and "true" in where_str:
            enabled_filter = True

        rows = self._rows_by_provider.get(pid_filter, []) if pid_filter else []
        if enabled_filter:
            rows = [r for r in rows if r.enabled]
        if aid_filter is not None:
            rows = [r for r in rows if r.account_id == aid_filter]

        return _FakeResult(rows)


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


def _make_fake_session(rows_by_provider):
    """Build a Session factory that returns a _FakeSession with the given rows."""

    def factory(*_args, **_kwargs):
        return _FakeSession(rows_by_provider=rows_by_provider)

    return factory


def test_resolve_single_row_no_account_id_returns_it():
    row = _row("anthropic", "default")
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [row]}),
    ):
        cfg = _resolve_provider_config("anthropic")
        assert cfg is row


def test_resolve_zero_rows_returns_none():
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": []}),
    ):
        assert _resolve_provider_config("anthropic") is None


def test_resolve_explicit_account_id_picks_specific_row():
    a = _row("anthropic", "default", enabled=True)
    b = _row("anthropic", "alice@example.com", enabled=True)
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        assert _resolve_provider_config("anthropic", account_id="alice@example.com") is b
        assert _resolve_provider_config("anthropic", account_id="default") is a


def test_resolve_multiple_rows_no_account_id_raises():
    a = _row("anthropic", "default", enabled=True)
    b = _row("anthropic", "alice@example.com", enabled=True)
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        with pytest.raises(AmbiguousProviderAccountError) as excinfo:
            _resolve_provider_config("anthropic")
        assert excinfo.value.provider_id == "anthropic"
        assert excinfo.value.account_count == 2


def test_resolve_ambiguous_skips_disabled_rows():
    """A disabled second account must not trigger ambiguity."""
    a = _row("anthropic", "default", enabled=True)
    b = _row("anthropic", "alice@example.com", enabled=False)
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        # `require_enabled=True` (default) — picks the only enabled row.
        assert _resolve_provider_config("anthropic") is a


def test_resolve_ambiguous_includes_disabled_when_not_required():
    a = _row("anthropic", "default", enabled=True)
    b = _row("anthropic", "alice@example.com", enabled=False)
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        with pytest.raises(AmbiguousProviderAccountError):
            # `require_enabled=False` — both rows count.
            _resolve_provider_config("anthropic", require_enabled=False)


def test_get_provider_api_key_picks_only_row():
    row = _row("anthropic", "default")
    row.api_key = "sk-ant-test"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [row]}),
    ):
        assert CredentialProvider.get_provider_api_key("anthropic") == "sk-ant-test"


def test_get_provider_api_key_picks_explicit_account():
    a = _row("anthropic", "default")
    a.api_key = "sk-default"
    b = _row("anthropic", "alice@example.com")
    b.api_key = "sk-alice"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        assert (
            CredentialProvider.get_provider_api_key("anthropic", account_id="alice@example.com")
            == "sk-alice"
        )
        assert (
            CredentialProvider.get_provider_api_key("anthropic", account_id="default")
            == "sk-default"
        )


def test_get_provider_api_key_ambiguous_raises():
    a = _row("anthropic", "default")
    a.api_key = "sk-default"
    b = _row("anthropic", "alice@example.com")
    b.api_key = "sk-alice"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"anthropic": [a, b]}),
    ):
        with pytest.raises(AmbiguousProviderAccountError):
            CredentialProvider.get_provider_api_key("anthropic")


def test_get_provider_session_cookie_picks_only_row():
    row = _row("kimi_coding", "default")
    row.session_cookie = "kimi-cookie-value"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"kimi_coding": [row]}),
    ):
        assert CredentialProvider.get_provider_session_cookie("kimi_coding") == "kimi-cookie-value"


def test_get_provider_session_cookie_picks_explicit_account():
    a = _row("kimi_coding", "default")
    a.session_cookie = "kimi-default"
    b = _row("kimi_coding", "bob@example.com")
    b.session_cookie = "kimi-bob"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"kimi_coding": [a, b]}),
    ):
        assert (
            CredentialProvider.get_provider_session_cookie(
                "kimi_coding", account_id="bob@example.com"
            )
            == "kimi-bob"
        )


def test_get_provider_session_cookie_ambiguous_raises():
    a = _row("kimi_coding", "default")
    a.session_cookie = "kimi-default"
    b = _row("kimi_coding", "bob@example.com")
    b.session_cookie = "kimi-bob"
    with patch(
        "app.services.credential_provider.Session",
        _make_fake_session({"kimi_coding": [a, b]}),
    ):
        with pytest.raises(AmbiguousProviderAccountError):
            CredentialProvider.get_provider_session_cookie("kimi_coding")


def test_get_credentials_with_explicit_account_id_picks_that_row():
    """`get_credentials` must thread the new `account_id` kwarg through."""
    a = _row("anthropic", "default")
    a.api_key = "sk-default"
    b = _row("anthropic", "alice@example.com")
    b.api_key = "sk-alice"
    with (
        patch(
            "app.services.credential_provider.Session",
            _make_fake_session({"anthropic": [a, b]}),
        ),
        patch.dict("os.environ", {}, clear=False),
        patch("app.services.credential_provider.registry.get_provider", return_value={"rules": []}),
    ):
        # With two rows + no account_id → ambiguous
        with pytest.raises(AmbiguousProviderAccountError):
            CredentialProvider.get_credentials("anthropic")

        # With explicit account_id → picks that row's api_key
        creds = CredentialProvider.get_credentials("anthropic", account_id="alice@example.com")
        assert creds.get("api_key") == "sk-alice"


def test_get_credentials_zero_rows_returns_empty_map():
    with (
        patch(
            "app.services.credential_provider.Session",
            _make_fake_session({"anthropic": []}),
        ),
        patch.dict("os.environ", {}, clear=False),
        patch("app.services.credential_provider.registry.get_provider", return_value={"rules": []}),
    ):
        creds = CredentialProvider.get_credentials("anthropic")
        # No api_key because no row; no env/file either
        assert creds.get("api_key") is None
