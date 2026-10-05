"""The server reads no credential files except Runway's own ``github_oauth.json`` (#551).

Credentials reach the server from sidecars, env vars and Settings. The server may run in
Docker with only its DB/config dir mounted, so a host-home read is both useless there and
a leak on a single-host install. These tests plant a valid credential file at every path
the registry's file rules name, under a throwaway ``HOME``, and prove the server never
looks at any of them. They never touch the real home directory.
"""

from __future__ import annotations

import ast
import builtins
import glob as glob_module
import json
import os
import re
from pathlib import Path

import pytest

from app.core.registry import registry
from app.services import credential_provider as cp
from app.services.credential_provider import CredentialProvider, _server_may_read

MARKER = "PLANTED-SECRET"
COLLECTORS_DIR = Path(__file__).resolve().parents[2] / "app" / "services" / "collectors"


def _file_rules():
    """Every ``(provider_id, rule)`` file rule in the registry the server does not own."""
    for provider_id in registry.get_all_providers():
        for rule in registry.get_provider(provider_id).get("rules", []):
            if rule.get("type") == "file" and not _server_may_read(rule):
                yield provider_id, rule


def _env_vars() -> set[str]:
    out = set()
    for provider_id in registry.get_all_providers():
        for rule in registry.get_provider(provider_id).get("rules", []):
            if rule.get("type") == "env" and rule.get("variable"):
                out.add(rule["variable"])
    return out


def _planted_payload(provider_id: str, rule: dict) -> dict:
    """A file body that WOULD yield a credential for every mapping key if it were read."""
    data: dict = {}
    for key_path, target in rule.get("mapping", {}).items():
        for alt in key_path.split("|"):
            data[alt.replace("*", "x")] = f"{MARKER}-{provider_id}-{target}"
    return data


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    """A throwaway HOME (and env dirs) with a valid file planted for every file rule."""
    home = tmp_path / "home"
    home.mkdir()
    runway_config = tmp_path / "runway-config"  # outside HOME on purpose
    runway_config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(runway_config))
    for var in _env_vars():
        monkeypatch.delenv(var, raising=False)
    env_dirs = {"CLAUDE_CONFIG_DIR": home / "claude-env", "CODEX_HOME": home / "codex-env"}
    for var, path in env_dirs.items():
        monkeypatch.setenv(var, str(path))

    planted: list[Path] = []
    for provider_id, rule in _file_rules():
        payload = json.dumps(_planted_payload(provider_id, rule))
        for raw in rule["paths"]:
            expanded = raw
            for var, path in env_dirs.items():
                expanded = expanded.replace("{{ENV_DIRS:" + var + "}}", str(path))
            target = Path(registry.resolve_path(expanded.replace("*", "x")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload)
            planted.append(target)
    assert planted, "the registry should declare sidecar-only file rules"
    return home, planted


def test_the_planted_files_would_be_credentials_if_the_server_read_them(hermetic_home, monkeypatch):
    """Control: with the gate off, the same planted files DO yield credentials. Without
    this the regression test below could pass for the wrong reason (a bad plant)."""
    monkeypatch.setattr(cp, "_server_may_read", lambda _rule: True)

    leaked = {
        provider_id
        for provider_id in registry.get_all_providers()
        if any(
            str(v).startswith(MARKER)
            for v in CredentialProvider.get_credentials(provider_id).values()
        )
    }

    assert {"gemini", "anthropic", "chatgpt", "github"} <= leaked


def test_the_server_reads_nothing_from_the_host_home(hermetic_home, monkeypatch):
    home, planted = hermetic_home
    prefix = str(home)
    touched: list[str] = []

    def spy(fn):
        def wrapper(path, *args, **kwargs):
            if isinstance(path, str | os.PathLike) and str(path).startswith(prefix):
                touched.append(str(path))
            return fn(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", spy(builtins.open))
    monkeypatch.setattr(os.path, "exists", spy(os.path.exists))
    monkeypatch.setattr(os.path, "isfile", spy(os.path.isfile))
    monkeypatch.setattr(os, "stat", spy(os.stat))
    monkeypatch.setattr(glob_module, "glob", spy(glob_module.glob))

    for provider_id in registry.get_all_providers():
        creds = CredentialProvider.get_credentials(provider_id)
        assert not [v for v in creds.values() if str(v).startswith(MARKER)], provider_id
        assert "server" not in set(creds.sources.values()), provider_id
        origins = CredentialProvider.server_credential_origins(provider_id)
        assert not [o for o in origins if o["source_type"] == "file"], provider_id

    from app.services.token_health import _scan_server_credentials

    assert _scan_server_credentials() == {}
    assert touched == [], f"the server touched the host home: {touched}"
    assert all(p.exists() for p in planted)  # nothing deleted or rewritten either


def test_runways_own_github_oauth_file_is_still_read(tmp_path, monkeypatch):
    """The one exception: the token the UI GitHub OAuth flow wrote into Runway's config dir."""
    runway_config = tmp_path / "runway-config"
    runway_config.mkdir()
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(runway_config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    (runway_config / "github_oauth.json").write_text(json.dumps({"access_token": "gho_runway"}))
    monkeypatch.setattr(cp.settings, "GITHUB_OAUTH_PATH", str(runway_config / "github_oauth.json"))

    creds = CredentialProvider.get_github_data()

    # pragma: allowlist nextline secret
    assert creds["api_key"] == "gho_runway"
    # pragma: allowlist nextline secret
    assert creds.sources["api_key"] == "config"
    (origin,) = [
        o
        for o in CredentialProvider.server_credential_origins("github")
        if o["source_type"] == "file"
    ]
    assert origin["label"] == "github_oauth.json"
    assert origin["managed"] is True


# os.path.expanduser / Path.home / os.getenv (a HOME read could flow into a non-``open`` sink)
_FORBIDDEN_CALLS = {"expanduser", "home", "getenv"}
_FORBIDDEN_MODULES = {"subprocess"}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _FORBIDDEN_CALLS or name.startswith("create_subprocess"):
                found.append(f"{path.name}:{node.lineno} calls {name}()")
        elif isinstance(node, ast.Attribute) and node.attr == "environ":
            found.append(f"{path.name}:{node.lineno} reads os.environ")
        elif isinstance(node, ast.Import):
            found += [
                f"{path.name}:{node.lineno} imports {a.name}"
                for a in node.names
                if a.name.split(".")[0] in _FORBIDDEN_MODULES
            ]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _FORBIDDEN_MODULES:
                found.append(f"{path.name}:{node.lineno} imports from {node.module}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.match(r"^~(/|\\|$)", node.value)
        ):
            found.append(f"{path.name}:{node.lineno} has a home-relative path {node.value!r}")
    return found


def test_no_collector_touches_the_host_home_or_runs_local_commands():
    """Static guard: collectors run on the server, so they must not expand ``~``, ask for the
    home directory or spawn processes. Local detection lives in the sidecar."""
    files = sorted(COLLECTORS_DIR.glob("*.py"))
    assert files
    assert [v for f in files for v in _violations(f)] == []


def test_the_static_guard_catches_what_it_is_meant_to(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os, subprocess\nfrom pathlib import Path\n"
        "a = os.path.expanduser('~')\nb = Path.home()\nc = '~/.gemini/x'\n"
        "d = os.getenv('HOME')\ne = os.environ['HOME']\n"
    )
    # import, expanduser/home/getenv calls, one "~" literal, one os.environ read
    assert len(_violations(bad)) == 7


def test_a_rule_the_server_skips_says_why(caplog):
    """A malformed rule must not look like an intentional sidecar-only skip."""
    with caplog.at_level("DEBUG", logger=cp.logger.name):
        assert not _server_may_read({"type": "file", "id": "broken"})
        assert not _server_may_read({"type": "file", "id": "gem", "paths": ["~/.gemini/x"]})
        assert _server_may_read(
            {"type": "file", "paths": ["{{CONFIG_DIR:runway}}/github_oauth.json"]}
        )
    reasons = [r.getMessage() for r in caplog.records]
    assert any("broken" in m and "no paths declared" in m for m in reasons)
    assert any("gem" in m and "outside the Runway config dir" in m for m in reasons)
