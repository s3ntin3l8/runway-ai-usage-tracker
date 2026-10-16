"""The demo instance's isolation guard (scripts/demo/env.py)."""

import socket
from pathlib import Path

import pytest

from scripts.demo.env import assert_demo_env, assert_ports_free, demo_env, demo_env_problems


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    return tmp_path / "demo"


def test_demo_env_inherits_only_the_allowlist(demo_dir: Path) -> None:
    parent = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "MULLION_SOCKET_PATH": "/run/mullion.sock",
        "RUNWAY_CONFIG_DIR": "/home/me/.config/runway",
        "ADMIN_API_KEY": "real",  # pragma: allowlist secret
        "GITHUB_TOKEN": "ghp_real",  # pragma: allowlist secret
        "OPENROUTER_API_KEY": "sk-real",  # pragma: allowlist secret
    }
    env = demo_env(demo_dir, parent=parent)
    assert env["PATH"] == "/usr/bin"
    assert "MULLION_SOCKET_PATH" not in env
    assert "ADMIN_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert env["RUNWAY_CONFIG_DIR"].startswith(str(demo_dir))
    assert env["BROWSER_PREFERENCE"] == ""
    assert demo_env_problems(env, demo_dir) == []


@pytest.mark.parametrize(
    "poison",
    [
        {"MULLION_SOCKET_PATH": "/x"},
        {"RUNWAY_CONFIG_DIR": "/home/me/.config/runway"},
        {"DATABASE_PATH": "/home/me/.config/runway/runway.db"},
        {"GITHUB_TOKEN": "ghp_x"},  # pragma: allowlist secret
        {"INGEST_API_KEY": "real"},  # pragma: allowlist secret
        {"DB_ENCRYPTION_KEY": "k"},  # pragma: allowlist secret
        {"OPENCODE_DB_PATH": "/home/me/.local/share/opencode/opencode.db"},
        {"HOME": "/home/me"},
        {"TRUSTED_PROXY_IPS": "10.0.0.1"},
        {"APP_HOST": "0.0.0.0"},
        {"APP_PORT": "8765"},
    ],
)
def test_assert_demo_env_refuses_a_poisoned_env(demo_dir: Path, poison: dict[str, str]) -> None:
    env = {**demo_env(demo_dir, parent={"PATH": "/usr/bin"}), **poison}
    assert demo_env_problems(env, demo_dir)
    with pytest.raises(RuntimeError, match="refused"):
        assert_demo_env(env, demo_dir)


def test_assert_demo_env_refuses_a_cwd_with_a_dotenv(demo_dir: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("ADMIN_API_KEY=real\n")  # pragma: allowlist secret
    env = demo_env(demo_dir, parent={"PATH": "/usr/bin"})
    assert any(".env" in p for p in demo_env_problems(env, demo_dir, cwd=cwd))


def test_assert_ports_free_rejects_a_listening_port() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            assert_ports_free([port])
