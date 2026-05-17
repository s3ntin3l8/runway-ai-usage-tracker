import pytest


def test_settings_type_coerces_int_fields():
    """BaseSettings auto-coerces string env vars to declared types."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("APP_PORT", "9001")
        mp.setenv("CLAUDE_PRO_LIMIT", "3000000")
        from app.core.config import Settings

        s = Settings()
        assert s.APP_PORT == 9001  # int, not "9001"
        assert s.CLAUDE_PRO_LIMIT == 3000000


def test_database_url_reflects_custom_path(tmp_path):
    """DATABASE_URL is computed from DATABASE_PATH (computed_field)."""
    db_file = str(tmp_path / "test.db")
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_PATH", db_file)
        from app.core.config import Settings

        s = Settings()
        assert f"sqlite:///{db_file}" == s.DATABASE_URL


def test_ingest_key_default_detection():
    """INGEST_API_KEY_IS_INSECURE_DEFAULT is True for the default key."""
    from app.core.config import DEFAULT_INGEST_API_KEY, Settings

    with pytest.MonkeyPatch().context() as mp:
        # Override env + .env file by forcing the default value explicitly
        mp.setenv("INGEST_API_KEY", DEFAULT_INGEST_API_KEY)
        s = Settings()
        assert s.INGEST_API_KEY == DEFAULT_INGEST_API_KEY
        assert s.INGEST_API_KEY_IS_INSECURE_DEFAULT is True


def test_ingest_key_custom_not_insecure():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("INGEST_API_KEY", "super-secret-custom-key")
        from app.core.config import Settings

        s = Settings()
        assert s.INGEST_API_KEY_IS_INSECURE_DEFAULT is False


def test_cors_origins_parses_env_var():
    """CORS_ORIGINS splits comma-separated env var into a list."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("CORS_ORIGINS", "http://app1.local,http://app2.local")
        from app.core.config import Settings

        s = Settings()
        assert s.CORS_ORIGINS == ["http://app1.local", "http://app2.local"]


def test_cors_origins_default_uses_app_port():
    """When CORS_ORIGINS env var not set and host is localhost, defaults use APP_PORT."""
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("CORS_ORIGINS", raising=False)
        mp.setenv("APP_HOST", "127.0.0.1")
        mp.setenv("APP_PORT", "9999")
        from app.core.config import Settings

        s = Settings()
        assert "http://localhost:9999" in s.CORS_ORIGINS


def test_cors_origins_returns_localhost_fallback_when_unset():
    """When CORS_ORIGINS is not in the env, the property falls back to the
    localhost pair. The non-localhost case is gated by
    _validate_security_invariants at startup, not by this property — see
    test_multi_host_gates.py."""
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("CORS_ORIGINS", raising=False)
        mp.setenv("APP_HOST", "127.0.0.1")
        from app.core.config import Settings

        s = Settings()
        assert s.CORS_ORIGINS == [
            f"http://localhost:{s.APP_PORT}",
            f"http://127.0.0.1:{s.APP_PORT}",
        ]


def test_log_format_defaults_to_plain():
    from app.core.config import Settings

    s = Settings()
    assert s.LOG_FORMAT == "plain"


def test_log_format_reads_env():
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("LOG_FORMAT", "json")
        from app.core.config import Settings

        s = Settings()
        assert s.LOG_FORMAT == "json"
