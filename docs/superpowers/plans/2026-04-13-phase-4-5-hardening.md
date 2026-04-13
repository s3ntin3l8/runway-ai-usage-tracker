# Phase 4.5 — Pre-Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete three 1.0 release prerequisites: type-safe Pydantic Settings, JSON structured logging, and SQLite snapshot compaction.

**Architecture:** Settings migrates to `pydantic_settings.BaseSettings` for type-safe env var loading. A `JsonFormatter` in `app/core/logging.py` is wired at startup when `LOG_FORMAT=json`. Compaction runs as a background daily job inside the existing poller, condensing old `usage_snapshots` rows to hourly (60–180d) and daily (180d+) buckets.

**Tech Stack:** `pydantic-settings` (already in requirements.txt), Python `logging`, SQLModel, `collections.defaultdict`

---

## File Map

| File | Action | Purpose |
|:---|:---|:---|
| `app/core/config.py` | Modify | Migrate to `BaseSettings`; add `LOG_FORMAT` field |
| `app/core/logging.py` | Create | `JsonFormatter` class |
| `app/main.py` | Modify | Wire `JsonFormatter` on startup |
| `app/services/compaction.py` | Create | `compact_snapshots()` function |
| `app/services/poller.py` | Modify | Add `_poll_count`; call compaction daily |
| `tests/unit/test_settings.py` | Create | Settings migration tests |
| `tests/unit/test_json_logging.py` | Create | JsonFormatter unit tests |
| `tests/unit/test_compaction.py` | Create | Compaction logic unit tests |

---

## Task 1: Migrate Settings to pydantic_settings.BaseSettings

**Files:**
- Modify: `app/core/config.py`
- Create: `tests/unit/test_settings.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_settings.py
import os
import pytest


def test_settings_type_coerces_int_fields():
    """BaseSettings auto-coerces string env vars to declared types."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("APP_PORT", "9001")
        mp.setenv("CLAUDE_PRO_LIMIT", "3000000")
        from app.core.config import Settings
        s = Settings()
        assert s.APP_PORT == 9001          # int, not "9001"
        assert s.CLAUDE_PRO_LIMIT == 3000000


def test_settings_type_coerces_bool_fields():
    """Boolean fields parse 'false' string correctly."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("LOCAL_COLLECTOR_ENABLED", "false")
        from app.core.config import Settings
        s = Settings()
        assert s.LOCAL_COLLECTOR_ENABLED is False


def test_database_url_reflects_custom_path(tmp_path):
    """DATABASE_URL is computed from DATABASE_PATH (computed_field)."""
    db_file = str(tmp_path / "test.db")
    with pytest.MonkeyPatch().context() as mp:
        mp.setenv("DATABASE_PATH", db_file)
        from app.core.config import Settings
        s = Settings()
        assert s.DATABASE_URL == f"sqlite:///{db_file}"


def test_ingest_key_default_detection():
    """INGEST_API_KEY_IS_INSECURE_DEFAULT is True for the default key."""
    from app.core.config import Settings, DEFAULT_INGEST_API_KEY
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
    """When CORS_ORIGINS env var not set, defaults use APP_PORT."""
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("CORS_ORIGINS", raising=False)
        mp.setenv("APP_PORT", "9999")
        from app.core.config import Settings
        s = Settings()
        assert "http://localhost:9999" in s.CORS_ORIGINS


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/unit/test_settings.py -v 2>&1 | head -40
```

Expected: multiple failures (Settings is not yet a BaseSettings subclass; no `LOG_FORMAT` field)

- [ ] **Step 3: Migrate app/core/config.py to BaseSettings**

Replace the entire file with:

```python
# app/core/config.py
import os
import logging
import platform
from typing import Optional, List
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def get_platform_data_dir(app_name: str) -> str:
    """Get the platform-specific directory for user data."""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return os.path.join(local_app_data, app_name)
        return os.path.join(home, "AppData", "Local", app_name)
    elif system == "Darwin":
        return os.path.join(home, "Library", "Application Support", app_name)
    else:
        xdg_data_home = os.getenv("XDG_DATA_HOME")
        if xdg_data_home:
            return os.path.join(xdg_data_home, app_name)
        return os.path.join(home, ".local", "share", app_name)


def get_platform_config_dir(app_name: str) -> str:
    """Get the platform-specific directory for user configuration."""
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return os.path.join(app_data, app_name)
        return os.path.join(home, "AppData", "Roaming", app_name)
    elif system == "Darwin":
        return os.path.join(home, "Library", "Application Support", app_name)
    else:
        xdg_config_home = os.getenv("XDG_CONFIG_HOME")
        if xdg_config_home:
            return os.path.join(xdg_config_home, app_name)
        return os.path.join(home, ".config", app_name)


DEFAULT_INGEST_API_KEY = "sidecar-default-secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Runway — AI Limits Dashboard"
    RUN_MODE: str = "standalone"

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = "Iv1.b507a08c87ecfe98"
    GITHUB_TOKEN: str = ""

    # Provider tokens
    CHATGPT_OAUTH_TOKEN: str = ""
    CLAUDE_CODE_OAUTH_TOKEN: str = ""
    OLLAMA_SESSION_TOKEN: str = ""
    OPENROUTER_API_KEY: str = ""
    MINIMAX_API_KEY: str = ""
    OPENCODE_GO_API_KEY: str = ""
    ZAI_API_KEY: str = ""
    KIMI_API_KEY: str = ""
    KIMI_AUTH_TOKEN: str = ""

    INGEST_API_KEY: str = DEFAULT_INGEST_API_KEY
    ADMIN_API_KEY: Optional[str] = None

    # OAuth credentials
    GEMINI_OAUTH_CLIENT_ID: str = ""
    GEMINI_OAUTH_CLIENT_SECRET: str = ""
    CLAUDE_OAUTH_CLIENT_ID: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

    KEYCHAIN_PROMPT_MODE: str = "always"

    # Quota limits
    CLAUDE_PRO_LIMIT: int = 2000000
    CLAUDE_FREE_LIMIT: int = 500000

    # Path settings — defaults computed at class load, overrideable via env var
    CLAUDE_PROJECTS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "projects")
    )
    CLAUDE_STATUSLINE_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "statusline.json")
    )
    GEMINI_SESSIONS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_data_dir("gemini"), "tmp", "sessions")
    )
    GEMINI_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("gemini"), "oauth_creds.json")
    )
    ANTHROPIC_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("claude"), "oauth_creds.json")
    )
    GITHUB_OAUTH_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_config_dir("usage-tracker"), "github_oauth.json"
        )
    )
    CHATGPT_AUTH_PATH: str = Field(
        default_factory=lambda: os.path.expanduser("~/.codex/auth.json")
    )
    CHATGPT_SESSIONS_DIR: str = Field(
        default_factory=lambda: os.path.join(get_platform_config_dir("codex"), "sessions")
    )
    ANTIGRAVITY_QUOTA_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_data_dir("antigravity"), "state", "quota.json"
        )
    )
    OPENCODE_DB_PATH: str = Field(
        default_factory=lambda: os.path.join(get_platform_data_dir("opencode"), "opencode.db")
    )
    DATABASE_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_config_dir("usage-tracker"), "runway.db"
        )
    )
    EXTERNAL_METRICS_PATH: str = Field(
        default_factory=lambda: os.path.join(
            get_platform_config_dir("usage-tracker"), "external_metrics.json"
        )
    )

    LOCAL_COLLECTOR_ENABLED: bool = True
    LOCAL_CREDENTIAL_SCRAPING_ENABLED: bool = True
    BROWSER_PREFERENCE: str = "safari,chrome,chromium,edge,firefox"

    # Network
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8765

    # Encryption
    DB_ENCRYPTION_KEY: Optional[str] = None

    # Logging format: "plain" (default) or "json"
    LOG_FORMAT: str = "plain"

    @property
    def INGEST_API_KEY_IS_INSECURE_DEFAULT(self) -> bool:
        return self.INGEST_API_KEY == DEFAULT_INGEST_API_KEY

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite:///{self.DATABASE_PATH}"

    @property
    def CORS_ORIGINS(self) -> List[str]:
        origins = os.getenv("CORS_ORIGINS")
        if origins:
            return [o.strip() for o in origins.split(",")]
        return [
            f"http://localhost:{self.APP_PORT}",
            f"http://127.0.0.1:{self.APP_PORT}",
        ]


settings = Settings()

# Security check: Warn if using default ingest secret
if settings.INGEST_API_KEY_IS_INSECURE_DEFAULT:
    logger.warning("=" * 60)
    logger.warning(
        "SECURITY WARNING: Using default INGEST_API_KEY ('sidecar-default-secret')"
    )
    logger.warning("The ingest endpoint is DISABLED until a custom key is set.")
    logger.warning("Set INGEST_API_KEY environment variable to a strong secret.")
    logger.warning("=" * 60)
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
source .venv/bin/activate && pytest tests/unit/test_settings.py -v
```

Expected: all 9 tests PASS

- [ ] **Step 5: Run full suite to check for regressions**

```bash
source .venv/bin/activate && pytest -x --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass (the ignored file has pre-existing macOS crypto failures unrelated to this change)

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py tests/unit/test_settings.py
git commit -m "feat: migrate Settings to pydantic_settings.BaseSettings with LOG_FORMAT field"
```

---

## Task 2: JSON Structured Logging

**Files:**
- Create: `app/core/logging.py`
- Modify: `app/main.py`
- Create: `tests/unit/test_json_logging.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_json_logging.py
import json
import logging
import sys
import pytest


def test_json_formatter_basic_fields():
    """JsonFormatter emits JSON with timestamp, level, logger, message."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.logger", level=logging.INFO,
        pathname="", lineno=0, msg="Hello world",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    data = json.loads(output)  # must be valid JSON
    assert data["level"] == "INFO"
    assert data["message"] == "Hello world"
    assert data["logger"] == "test.logger"
    assert "timestamp" in data
    assert "exc_info" not in data  # no exception present


def test_json_formatter_with_exception():
    """exc_info key is present and contains traceback when exception attached."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        ei = sys.exc_info()
    record = logging.LogRecord(
        name="test.logger", level=logging.ERROR,
        pathname="", lineno=0, msg="Error occurred",
        args=(), exc_info=ei,
    )
    output = formatter.format(record)
    data = json.loads(output)
    assert "exc_info" in data
    assert "ValueError" in data["exc_info"]
    assert "boom" in data["exc_info"]


def test_json_formatter_message_args_interpolated():
    """printf-style message args are interpolated into the message."""
    from app.core.logging import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x", level=logging.DEBUG,
        pathname="", lineno=0, msg="value is %d",
        args=(42,), exc_info=None,
    )
    data = json.loads(formatter.format(record))
    assert data["message"] == "value is 42"


def test_json_formatter_timestamp_is_iso8601():
    """Timestamp is a valid ISO 8601 string ending in +00:00 or Z."""
    from app.core.logging import JsonFormatter
    from datetime import datetime
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="x", level=logging.INFO,
        pathname="", lineno=0, msg="ts test",
        args=(), exc_info=None,
    )
    data = json.loads(formatter.format(record))
    # Should parse without error
    datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/unit/test_json_logging.py -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'JsonFormatter' from 'app.core.logging'`

- [ ] **Step 3: Create app/core/logging.py**

```python
# app/core/logging.py
import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry)
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
source .venv/bin/activate && pytest tests/unit/test_json_logging.py -v
```

Expected: all 4 tests PASS

- [ ] **Step 5: Wire JsonFormatter into main.py startup**

In `app/main.py`, replace the existing logging setup block:

```python
# OLD (remove this block):
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Silence noisy httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
```

With:

```python
# NEW:
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
# Silence noisy httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)
# Apply JSON formatter when LOG_FORMAT=json
if settings.LOG_FORMAT == "json":
    from app.core.logging import JsonFormatter
    _json_fmt = JsonFormatter()
    for _handler in logging.root.handlers:
        _handler.setFormatter(_json_fmt)
logger = logging.getLogger(__name__)
```

Note: `settings` is already imported at the top of `main.py`. No new import needed.

- [ ] **Step 6: Smoke-test the JSON formatter wiring**

```bash
source .venv/bin/activate && LOG_FORMAT=json python -c "
import logging, sys
logging.basicConfig(handlers=[logging.StreamHandler(sys.stdout)])
from app.core.config import settings
if settings.LOG_FORMAT == 'json':
    from app.core.logging import JsonFormatter
    for h in logging.root.handlers:
        h.setFormatter(JsonFormatter())
logging.getLogger('test').info('json logging works')
" 2>&1
```

Expected output: a single JSON line like `{"timestamp": "...", "level": "INFO", "logger": "test", "message": "json logging works"}`

- [ ] **Step 7: Run full suite**

```bash
source .venv/bin/activate && pytest -x --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass

- [ ] **Step 8: Commit**

```bash
git add app/core/logging.py app/main.py tests/unit/test_json_logging.py
git commit -m "feat: add JsonFormatter for LOG_FORMAT=json structured logging"
```

---

## Task 3: Data Retention Compaction Service

**Files:**
- Create: `app/services/compaction.py`
- Create: `tests/unit/test_compaction.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_compaction.py
import pytest
from datetime import datetime, timedelta, timezone
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool
from app.models.db import UsageSnapshot


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _snap(ts, used, limit, provider="anthropic", account="acc1"):
    """Helper: create a UsageSnapshot with raw_metadata_json set (not yet compacted)."""
    return UsageSnapshot(
        timestamp=ts,
        provider_id=provider,
        account_id=account,
        service_name="Test Service",
        used_value=used,
        limit_value=limit,
        unit_type="tokens",
        health="good",
        data_source="oauth",
        window_type="monthly",
        raw_metadata_json='{"test": true}',  # non-NULL = not compacted
    )


def test_hourly_compaction_merges_rows(session):
    """Rows 60-180 days old are compacted to one row per hour-bucket."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    for used in [100.0, 200.0, 300.0]:
        session.add(_snap(old, used, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 1
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].used_value == pytest.approx(200.0)  # avg(100, 200, 300)
    assert rows[0].raw_metadata_json is None  # compacted marker


def test_daily_compaction_merges_rows(session):
    """Rows 180+ days old are compacted to one row per day-bucket."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=200)
    for used in [50.0, 150.0]:
        session.add(_snap(old, used, 500.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["daily_compacted"] == 1
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].used_value == pytest.approx(100.0)  # avg(50, 150)


def test_recent_rows_not_compacted(session):
    """Rows less than 60 days old are left untouched."""
    from app.services.compaction import compact_snapshots

    recent = datetime.now(timezone.utc) - timedelta(days=10)
    session.add(_snap(recent, 100.0, 1000.0))
    session.add(_snap(recent, 200.0, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert result["daily_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 2


def test_already_compacted_rows_skipped(session):
    """Rows with raw_metadata_json=NULL are not re-compacted."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    snap = _snap(old, 100.0, 1000.0)
    snap.raw_metadata_json = None  # already compacted
    session.add(snap)
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 1  # untouched


def test_single_row_per_bucket_not_compacted(session):
    """A bucket with only one row is not touched."""
    from app.services.compaction import compact_snapshots

    # Two rows in DIFFERENT hours
    old1 = datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
    old2 = datetime(2025, 1, 1, 11, 0, tzinfo=timezone.utc)
    session.add(_snap(old1, 100.0, 1000.0))
    session.add(_snap(old2, 200.0, 1000.0))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 0
    assert len(session.exec(select(UsageSnapshot)).all()) == 2


def test_multiple_providers_compacted_independently(session):
    """Different providers are compacted into separate rows."""
    from app.services.compaction import compact_snapshots

    old = datetime.now(timezone.utc) - timedelta(days=90)
    for provider in ["anthropic", "openai"]:
        for used in [100.0, 200.0]:
            session.add(_snap(old, used, 1000.0, provider=provider))
    session.commit()

    result = compact_snapshots(session)

    assert result["hourly_compacted"] == 2
    rows = session.exec(select(UsageSnapshot)).all()
    assert len(rows) == 2
    providers = {r.provider_id for r in rows}
    assert providers == {"anthropic", "openai"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
source .venv/bin/activate && pytest tests/unit/test_compaction.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'app.services.compaction'`

- [ ] **Step 3: Create app/services/compaction.py**

```python
# app/services/compaction.py
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlmodel import Session, select
from app.models.db import UsageSnapshot

logger = logging.getLogger(__name__)

_HOURLY_DAYS = 60    # compact to hourly: rows older than 60 days
_DAILY_DAYS = 180    # compact to daily:  rows older than 180 days


def compact_snapshots(session: Session) -> dict:
    """
    Downsample old usage_snapshots to reduce DB size.

    Thresholds:
      - 60–180 days old  → one averaged row per (provider, account, model, window, hour)
      - 180+ days old    → one averaged row per (provider, account, model, window, day)

    Compacted rows are marked with raw_metadata_json = NULL.
    Rows already marked NULL are skipped (never re-compacted).

    Returns: {"hourly_compacted": N, "daily_compacted": N}
    """
    now = datetime.now(timezone.utc)
    hourly_threshold = now - timedelta(days=_HOURLY_DAYS)
    daily_threshold = now - timedelta(days=_DAILY_DAYS)

    hourly_count = _compact_range(
        session,
        start=daily_threshold,
        end=hourly_threshold,
        bucket_fn=lambda ts: ts.strftime("%Y-%m-%d %H"),
    )
    daily_count = _compact_range(
        session,
        start=None,
        end=daily_threshold,
        bucket_fn=lambda ts: ts.strftime("%Y-%m-%d"),
    )
    return {"hourly_compacted": hourly_count, "daily_compacted": daily_count}


def _compact_range(
    session: Session,
    start: Optional[datetime],
    end: datetime,
    bucket_fn,
) -> int:
    """Compact rows in [start, end) into time buckets. Returns number of new rows created."""
    stmt = (
        select(UsageSnapshot)
        .where(UsageSnapshot.timestamp < end)
        .where(UsageSnapshot.raw_metadata_json != None)  # noqa: E711
    )
    if start is not None:
        stmt = stmt.where(UsageSnapshot.timestamp >= start)

    rows = session.exec(stmt).all()
    if not rows:
        return 0

    groups: dict[tuple, list[UsageSnapshot]] = defaultdict(list)
    for row in rows:
        key = (
            row.provider_id,
            row.account_id,
            row.model_id,
            row.window_type,
            bucket_fn(row.timestamp),
        )
        groups[key].append(row)

    created = 0
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue  # single row — no compaction needed

        used_vals = [r.used_value for r in group_rows if r.used_value is not None]
        limit_vals = [r.limit_value for r in group_rows if r.limit_value is not None]
        avg_used = sum(used_vals) / len(used_vals) if used_vals else None
        avg_limit = sum(limit_vals) / len(limit_vals) if limit_vals else None

        template = group_rows[0]

        for row in group_rows:
            session.delete(row)

        session.add(
            UsageSnapshot(
                timestamp=template.timestamp,
                provider_id=template.provider_id,
                account_id=template.account_id,
                account_label=template.account_label,
                service_name=template.service_name,
                used_value=avg_used,
                limit_value=avg_limit,
                unit_type=template.unit_type,
                currency=template.currency,
                tier=template.tier,
                model_id=template.model_id,
                window_type=template.window_type,
                health=template.health,
                sidecar_id=template.sidecar_id,
                is_unlimited=template.is_unlimited,
                data_source=template.data_source,
                error_type=template.error_type,
                raw_metadata_json=None,  # compacted marker
            )
        )
        created += 1

    session.commit()
    logger.info(f"Compaction complete: {created} buckets merged")
    return created
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
source .venv/bin/activate && pytest tests/unit/test_compaction.py -v
```

Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/compaction.py tests/unit/test_compaction.py
git commit -m "feat: add data retention compaction service (60d hourly, 180d daily)"
```

---

## Task 4: Wire Compaction into Background Poller

**Files:**
- Modify: `app/services/poller.py`

- [ ] **Step 1: Update BackgroundPoller to track poll count and trigger daily compaction**

In `app/services/poller.py`, make these targeted changes:

```python
# app/services/poller.py
import asyncio
import logging
from datetime import datetime, timezone
from sqlmodel import Session
from app.services.collector_manager import manager
from app.core.db import engine
from app.models.db import UsageSnapshot
from app.models.schemas import LimitCard
from typing import List, Optional

logger = logging.getLogger(__name__)

_COMPACTION_INTERVAL_POLLS = 96  # 96 × 15 min = 24 hours


class BackgroundPoller:
    def __init__(self, interval_seconds: int = 900):  # Default 15 minutes
        self.interval = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._poll_count = 0  # tracks polls for daily compaction trigger

    def start(self):
        """Start the background polling task."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Background poller started with {self.interval}s interval.")

    async def stop(self):
        """Stop the background polling task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background poller stopped.")

    async def _run_loop(self):
        while self._running:
            await asyncio.sleep(self.interval)
            if not self._running:
                break
            try:
                await self.poll_now()
            except Exception as e:
                logger.error(f"Error during background poll: {e}")

    async def poll_now(self):
        """Execute a single collection and snapshot cycle."""
        logger.info("Starting scheduled background collection...")
        cards = await manager.collect_all()

        if not cards:
            logger.debug("No metrics collected during background poll.")
            return

        with Session(engine) as session:
            for card_dict in cards:
                try:
                    card = LimitCard(**card_dict)
                    if not card.provider_id or not card.account_id:
                        continue
                    if card.data_source == "cache":
                        continue
                    snapshot = UsageSnapshot(
                        provider_id=card.provider_id,
                        account_id=card.account_id,
                        account_label=card.account_label,
                        service_name=card.service_name,
                        used_value=card.used_value,
                        limit_value=card.limit_value,
                        unit_type=card.unit_type,
                        currency=card.currency,
                        tier=card.tier,
                        model_id=card.model_id,
                        window_type=card.window_type,
                        health=card.health,
                        sidecar_id=card.sidecar_id,
                        is_unlimited=card.is_unlimited,
                        data_source=card.data_source,
                        error_type=card.error_type,
                        timestamp=datetime.now(timezone.utc),
                    )
                    snapshot.raw_metadata = card.metadata
                    session.add(snapshot)
                except Exception as e:
                    logger.error(f"Failed to map card to snapshot: {e}")

            session.commit()
            logger.info(f"Background poll complete. Snapshotted {len(cards)} metrics.")

        # Run daily compaction every 96 polls (≈ 24 h at 15-min interval)
        self._poll_count += 1
        if self._poll_count % _COMPACTION_INTERVAL_POLLS == 0:
            try:
                from app.services.compaction import compact_snapshots
                with Session(engine) as compact_session:
                    result = compact_snapshots(compact_session)
                    logger.info(f"Daily compaction result: {result}")
            except Exception as e:
                logger.error(f"Compaction failed (non-fatal): {e}")


# Global instance
poller = BackgroundPoller()
```

- [ ] **Step 2: Run full test suite**

```bash
source .venv/bin/activate && pytest -x --ignore=tests/unit/test_browser_cookies.py -q
```

Expected: all tests pass

- [ ] **Step 3: Commit**

```bash
git add app/services/poller.py
git commit -m "feat: trigger daily snapshot compaction in background poller"
```

---

## Task 5: Final Verification

- [ ] **Step 1: Run the complete test suite**

```bash
source .venv/bin/activate && pytest --ignore=tests/unit/test_browser_cookies.py -v
```

Expected: all tests pass (the 2 ignored macOS crypto tests are pre-existing)

- [ ] **Step 2: Verify JSON logging works end-to-end**

```bash
source .venv/bin/activate && LOG_FORMAT=json uvicorn app.main:app --port 8765 2>&1 | head -5
```

Expected: startup log lines are valid JSON objects, e.g.:
```json
{"timestamp": "2026-04-13T...", "level": "INFO", "logger": "app.main", "message": "..."}
```

- [ ] **Step 3: Commit Phase 4.5 completion marker in ideas.md**

In `docs/ideas.md`, under the 1.0 Release Prerequisites section, mark these items complete:
- `- [x] **Structured Logging:** Add JSON logging formatter option`
- `- [x] **Pydantic Settings (A1):** Refactor Settings to extend pydantic_settings.BaseSettings`
- `- [x] **Data Retention Compaction:** Background job to downsample snapshots older than 60 days`

```bash
git add docs/ideas.md
git commit -m "Phase 4.5: Pydantic Settings, JSON logging, and snapshot compaction complete"
```
