"""
Pytest configuration and shared fixtures for AI Usage Tracker tests.

Provides:
- Async test support via pytest-asyncio
- Mock httpx.AsyncClient for API testing
- Temporary environment variables for isolation
- Mock response data for various providers
"""

import atexit
import getpass
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _pytest_user() -> str:
    """The name pytest files its per-run temp directories under."""
    try:
        return getpass.getuser() or "unknown"
    except (ImportError, KeyError, OSError):
        return "unknown"


def _pytest_root_dir(temproot: Path) -> Path:
    """Where pytest keeps its per-run temp directories."""
    return temproot / f"pytest-of-{_pytest_user()}"


def _current_uid() -> int | None:
    """The current user's id, or None on a platform that has no such concept."""
    if os.name == "nt":
        return None
    try:
        return os.getuid()
    except AttributeError:  # pragma: no cover - platform dependent
        return None


def _pytest_root_usable(pytest_root: Path) -> bool:
    """Whether pytest can use this root for its per-run directories.

    Mirrors pytest's own rules (``_pytest/tmpdir.py``): the root must not be
    a symlink, must belong to the current user, and must accept a new
    directory. The write is actually attempted — a root left behind by an
    elevated process is typically readable but *not* writable, and a read
    check would wave it through.

    A missing root is fine: pytest creates it itself.
    """
    try:
        info = pytest_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        try:
            pytest_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return False
        try:
            info = pytest_root.stat(follow_symlinks=False)
        except OSError:
            return False
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    uid = _current_uid()
    if uid is not None and info.st_uid != uid:
        return False
    # A direct mkdir, not tempfile.mkdtemp: on Windows tempfile retries a
    # PermissionError up to TMP_MAX times (it cannot tell an unusable
    # directory from a reserved device name), which turns "this directory is
    # unusable" into a multi-minute hang.
    probe = pytest_root / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        probe.mkdir()
    except OSError:
        return False
    try:
        probe.rmdir()
    except OSError:
        return False
    return True


def _ensure_writable_pytest_root(temproot: Path | None = None) -> None:
    """Point pytest at a temp root this user can actually use.

    pytest keeps its per-run directories in ``%TEMP%\\pytest-of-<user>``. A
    directory left behind by another user's (or an elevated) process can carry
    an ACL that leaves this user unable to use it — pytest then fails *every*
    test with ``PermissionError`` before a single one runs. The repo's own
    runner sidesteps this with a unique ``--basetemp``
    (``scripts/dev_checks.py``); this keeps a bare ``pytest`` working too.

    The fallback is a fresh directory per run, not a shared one: pytest
    allocates its per-run directories under the root it is given, and a
    per-run root keeps concurrent runs from sharing anything. The cost is
    that a killed run can leave its fallback behind — pytest's own numbered
    directories are cleaned up on exit, this one only on a normal exit.
    """
    root = Path(tempfile.gettempdir()) if temproot is None else temproot
    if _pytest_root_usable(_pytest_root_dir(root)):
        return
    fallback = root / f"runway-pytest-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        fallback.mkdir()
    except OSError:
        return  # Nothing left to try — let pytest report the real error.
    # PYTEST_DEBUG_TEMPROOT is the env var pytest reads for its temp root.
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(fallback)
    atexit.register(shutil.rmtree, fallback, True)


_ensure_writable_pytest_root()

# Force a localhost / un-secured configuration for tests, BEFORE any app
# module is imported. config.py runs _validate_security_invariants at import
# time, so an operator's .env that legitimately sets APP_HOST=0.0.0.0 would
# otherwise refuse to load the test suite. Tests that want to exercise the
# multi-host gates monkeypatch the live settings explicitly.
os.environ["APP_HOST"] = "127.0.0.1"
os.environ.pop("CORS_ORIGINS", None)
os.environ.pop("TLS_TERMINATED", None)
# An operator's .env may configure the admin and encryption keys. The
# EncryptionService singleton is built from them when the app is imported, and
# several modules hold that instance, so the suite has to neutralise them
# before the import instead of patching them afterwards. An empty value
# normalises to None (config.py), which keeps every keyless path keyless and
# leaves a keyed checkout behaving exactly like CI.
os.environ["ADMIN_API_KEY"] = ""
os.environ["DB_ENCRYPTION_KEY"] = ""
# pystray's dummy backend allows tray menu tests on headless Linux CI.
os.environ["PYSTRAY_BACKEND"] = "dummy"

import httpx
import pytest

from app.core.config import Settings, settings
from app.core.net import LOOPBACK_HOSTS
from app.main import app
from app.services.token_cache import token_cache
from tests.fixtures.mock_data import (
    ANTHROPIC_OAUTH_RESPONSE,
    BIGMODEL_ZAI_RESPONSE,
    CHATGPT_USAGE_RESPONSE,
    CLAUDE_WEB_API_ORGS_RESPONSE,
    CLAUDE_WEB_API_USAGE_RESPONSE,
    GEMINI_QUOTA_RESPONSE,
    KIMI_RESPONSE,
    OPENCODE_GO_RESPONSE,
)


@pytest.fixture(autouse=True)
def setup_test_settings(monkeypatch):
    """Ensure consistent settings for all tests, isolating them from the local .env."""
    monkeypatch.setattr(settings, "ADMIN_API_KEY", None)
    monkeypatch.setattr(
        "app.core.http_security.local_request_hosts",
        lambda: LOOPBACK_HOSTS | frozenset({"testserver", "test"}),
    )
    # An operator's .env may configure real forward-auth values (e.g. Authentik
    # header names, TRUSTED_PROXY_IPS) for their deployment; reset to the
    # Settings model's own defaults (not re-hardcoded here) so proxy-trust
    # tests aren't at the mercy of the local environment.
    for field in (
        "TRUSTED_PROXY_IPS",
        "FORWARD_AUTH_USER_HEADER",
        "FORWARD_AUTH_EMAIL_HEADER",
        "FORWARD_AUTH_GROUPS_HEADER",
        "FORWARD_AUTH_ALLOWED_GROUPS",
        "FORWARD_AUTH_ALLOWED_USERS",
        # The encryption key is neutralised before the app import above, so the
        # singleton and the modules holding it stay keyless; resetting the
        # setting keeps the per-test state at the model default as well.
        "DB_ENCRYPTION_KEY",
    ):
        monkeypatch.setattr(settings, field, Settings.model_fields[field].default)


@pytest.fixture(autouse=True)
async def clear_token_cache():
    """Clear the global token cache before each test to ensure isolation."""
    await token_cache.reset()


@pytest.fixture(autouse=True)
def isolate_token_health_state():
    """Keep Token Health tests independent of the host machine.

    ``get_health`` scans the server's own env vars / credential files and reads
    the process-wide auth-failure registry; without this, whatever the dev box
    exports (``GITHUB_TOKEN`` ...) or a previous test flagged would leak in.
    Tests exercising either path override the patch or mark the registry.
    """
    from app.services import auth_failures

    auth_failures.reset()
    with (
        patch("app.services.token_health._collect_server_credentials", return_value={}),
    ):
        yield
    auth_failures.reset()


@pytest.fixture(autouse=True)
def clear_response_cache():
    """Clear the process-wide TTL response cache before each test.

    app/core/cache.py backs resolve_user_tz + the /fleet, /global-stats,
    /top-*, and /forecast response caches with a module-level dict keyed by
    strings like "fleet" or "user_tz" — without this, a value cached by one
    test (e.g. a resolved timezone) would leak into the next test's assertions.
    """
    from app.core.cache import cache_clear

    cache_clear()
    yield
    cache_clear()


@pytest.fixture(autouse=True)
def mock_db_session(monkeypatch):
    """Mock DB access without leaking patched Session classes across tests."""
    from sqlmodel import Session

    mock_session = MagicMock()
    mock_db = MagicMock()
    mock_db.exec.return_value.all.return_value = []
    mock_db.exec.return_value.first.return_value = None
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_session.return_value.__enter__.return_value = mock_db
    # Share the undo stack with tests which temporarily restore real Session.
    # A separate patch context can otherwise undo before monkeypatch teardown,
    # leaving its mock installed globally for subsequent tests.
    monkeypatch.setattr("sqlmodel.Session", mock_session)
    yield mock_db
    for module in tuple(sys.modules.values()):
        if module is not None and vars(module).get("Session") is mock_session:
            module.Session = Session


@pytest.fixture
def env_vars(monkeypatch):
    """Provide a helper to set environment variables during tests using monkeypatch."""

    def set_vars(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, value)

    return set_vars


@pytest.fixture
async def api_client():
    """Provide an asynchronous httpx client for testing the FastAPI application."""
    async with httpx.AsyncClient(app=app, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def mock_http_client():
    """Provide a mock httpx.AsyncClient for testing API calls."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def temp_env_file():
    """Create a temporary .env file for testing configuration loading."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("CLAUDE_CODE_OAUTH_TOKEN=test_token\n")
        f.write("GITHUB_TOKEN=github_test_token\n")
        temp_path = f.name

    yield temp_path

    # Cleanup
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def mock_anthropic_oauth_response():
    """Mock response from Anthropic OAuth API."""
    return ANTHROPIC_OAUTH_RESPONSE


@pytest.fixture
def mock_claude_web_api_orgs_response():
    """Mock response from Claude Web API organizations endpoint."""
    return CLAUDE_WEB_API_ORGS_RESPONSE


@pytest.fixture
def mock_claude_web_api_usage_response():
    """Mock response from Claude Web API usage endpoint."""
    return CLAUDE_WEB_API_USAGE_RESPONSE


@pytest.fixture
def mock_gemini_quota_response():
    """Mock response from Gemini quota API."""
    return GEMINI_QUOTA_RESPONSE


@pytest.fixture
def mock_chatgpt_usage_response():
    """Mock response from ChatGPT wham/usage API."""
    return CHATGPT_USAGE_RESPONSE


@pytest.fixture
def mock_opencode_go_response():
    """Mock response from OpenCode Go API."""
    return OPENCODE_GO_RESPONSE


@pytest.fixture
def mock_zai_response():
    """Mock response from zAI (Zhipu) API."""
    return BIGMODEL_ZAI_RESPONSE


@pytest.fixture
def mock_kimi_response():
    """Mock response from Kimi (Moonshot) API."""
    return KIMI_RESPONSE


@pytest.fixture
def error_response():
    """Provide common error response mocks."""

    def _create(status_code=500, detail="Internal Server Error"):
        response = MagicMock(spec=httpx.Response)
        response.status_code = status_code
        response.json.return_value = {"detail": detail}
        response.text = json.dumps({"detail": detail})
        return response

    return _create


@pytest.fixture
def mock_http_response(monkeypatch):
    """Factory fixture to create mock httpx.Response objects."""

    def create_response(status_code=200, json_data=None, text=""):
        response = MagicMock(spec=httpx.Response)
        response.status_code = status_code
        response.json.return_value = json_data or {}
        response.text = text
        return response

    return create_response


@pytest.fixture
def mock_keyring():
    """Provide a mock keyring module for testing token retrieval."""
    with patch("app.core.config.keyring") as mock_keyring:
        mock_keyring.get_password.return_value = None
        yield mock_keyring


# Pytest asyncio configuration
def pytest_configure(config):
    """Configure pytest with asyncio mode."""
    config.addinivalue_line("markers", "asyncio: mark test as asyncio")


@pytest.fixture(autouse=True)
def isolated_replay_receipts(monkeypatch, tmp_path):
    """Replay auth must never read/write the developer's real database."""
    from sqlmodel import create_engine

    from app.models.db import SidecarRequestReceipt

    replay_engine = None

    def get_engine():
        nonlocal replay_engine
        if replay_engine is None:
            replay_engine = create_engine(
                f"sqlite:///{tmp_path / 'replay.db'}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            SidecarRequestReceipt.__table__.create(replay_engine)
        return replay_engine

    monkeypatch.setattr("app.core.replay.receipt_engine", get_engine)
    yield
    if replay_engine is not None:
        replay_engine.dispose()


@pytest.fixture
def make_symlink():
    """Skip only a missing Windows symlink privilege, never an application failure."""

    def create(link, target, *, target_is_directory=None):
        try:
            link.symlink_to(
                target,
                target_is_directory=target.is_dir()
                if target_is_directory is None
                else target_is_directory,
            )
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip(
                    "Windows symlink privilege unavailable (Developer Mode or elevation required)"
                )
            raise

    return create
