"""The sidecar's baked registry is generated from registry.json -- and stays that way.

Drift used to be silent: the baked copy and ``app/core/registry.json`` disagreed on
cookie targets, the keychain field name, rule types and more, with only a few
hand-written pin tests guarding single fields. These tests compare the whole.
"""

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import gen_sidecar_registry as gen  # noqa: E402
from scripts import sidecar  # noqa: E402

REGISTRY = json.loads(gen.REGISTRY_PATH.read_text(encoding="utf-8"))
OVERLAY = json.loads(gen.OVERLAY_PATH.read_text(encoding="utf-8"))


def test_baked_registry_equals_generator_output():
    """Fails when registry.json, the overlay or the baked block is edited alone."""
    assert sidecar.__REGISTRY__ == gen.build_registry(), "run `make sidecar-registry`"


def test_baked_block_text_is_what_the_generator_writes():
    """The generated text survives ruff format, so `make sidecar-registry` is a no-op."""
    source = (ROOT / "scripts" / "sidecar.py").read_text(encoding="utf-8")
    assert gen.apply(source) == source, "run `make sidecar-registry`"
    assert gen.main(["--check"]) == 0


def test_every_server_provider_reaches_the_sidecar():
    assert set(REGISTRY["providers"]) <= set(sidecar.__REGISTRY__["providers"])


def test_only_the_overlay_adds_sidecar_only_providers():
    extra = set(sidecar.__REGISTRY__["providers"]) - set(REGISTRY["providers"])
    assert extra == set(OVERLAY["add_providers"])


def test_every_overlay_entry_states_a_reason():
    for pid, changes in OVERLAY["providers"].items():
        for key in ("drop_rules", "drop_mapping_keys", "add_rules"):
            for entry in changes.get(key, []):
                assert entry.get("reason", "").strip(), f"{pid}.{key} entry has no reason"
        if "set" in changes:
            assert changes.get("set_reason", "").strip(), f"{pid}.set has no reason"
    for pid, add in OVERLAY["add_providers"].items():
        assert add.get("reason", "").strip(), f"add_providers.{pid} has no reason"


def test_a_stale_overlay_entry_is_an_error_not_a_silent_noop():
    stale = copy.deepcopy(OVERLAY)
    stale["providers"]["anthropic"]["drop_rules"][0]["match"]["variable"] = "NO_SUCH_VAR"
    with pytest.raises(ValueError, match="matched nothing"):
        gen.build_registry(REGISTRY, stale)

    redundant = copy.deepcopy(OVERLAY)
    redundant["providers"]["github"]["set"]["name"] = REGISTRY["providers"]["github"]["name"]
    with pytest.raises(ValueError, match="already"):
        gen.build_registry(REGISTRY, redundant)

    partly = copy.deepcopy(OVERLAY)
    partly["providers"]["github"]["drop_mapping_keys"][0]["keys"].append("github.com.nope")
    with pytest.raises(ValueError, match="matched nothing"):
        gen.build_registry(REGISTRY, partly)

    unknown = copy.deepcopy(OVERLAY)
    unknown["providers"]["nope"] = {"set": {}}
    with pytest.raises(ValueError, match="unknown provider"):
        gen.build_registry(REGISTRY, unknown)


def test_every_baked_rule_type_is_one_the_sidecar_can_run():
    source = (ROOT / "scripts" / "sidecar.py").read_text(encoding="utf-8")
    implemented = set(re.findall(r'rule_type == "([a-z_]+)"', source))
    used = {r["type"] for p in sidecar.__REGISTRY__["providers"].values() for r in p["rules"]}
    assert used <= implemented, f"sidecar silently skips: {sorted(used - implemented)}"


def test_keychain_rules_use_the_field_the_sidecar_reads():
    for pid, provider in sidecar.__REGISTRY__["providers"].items():
        for rule in provider["rules"]:
            if rule["type"] == "keychain":
                assert rule.get("service_name"), pid
                assert "service" not in rule, pid


def test_cookie_credentials_use_the_redacted_session_cookie_key():
    """`session_cookie` is a known credential key (redaction, ingest); `cookie_session`
    and `cookie_kimi-auth` were per-side spellings of it."""
    for pid in ("kimi_coding", "ollama"):
        targets = {
            r["mapping"]["value"]
            for r in sidecar.__REGISTRY__["providers"][pid]["rules"]
            if r["type"] == "cookie" or (r["type"] == "env" and "SESSION" in r["variable"])
        }
        assert targets == {"session_cookie"}, pid


def test_github_hosts_yml_maps_the_token_but_not_the_login():
    rules = [
        r
        for r in sidecar.__REGISTRY__["providers"]["github"]["rules"]
        if r["type"] == "file" and r["format"] == "yaml"
    ]
    assert len(rules) == 1
    assert rules[0]["mapping"] == {"github.com.oauth_token": "api_key"}
    # The server still maps it; the divergence is deliberate and recorded in the overlay.
    assert "github.com.user" in json.dumps(REGISTRY["providers"]["github"])


def test_server_only_env_session_tokens_stay_out_of_the_sidecar():
    sidecar_env = {
        r["variable"]
        for p in sidecar.__REGISTRY__["providers"].values()
        for r in p["rules"]
        if r["type"] == "env"
    }
    assert not {"CLAUDE_SESSION_TOKEN", "CHATGPT_SESSION_TOKEN"} & sidecar_env


def test_icons_are_real_characters_not_lone_surrogates():
    for pid, provider in sidecar.__REGISTRY__["providers"].items():
        provider["icon"].encode("utf-8")  # raises on a lone surrogate


def test_fingerprint_helpers_agree_between_sidecar_and_server():
    """The two copies have to match byte for byte or origins stop lining up."""
    from app.services import account_identity as server
    from scripts.sidecar_pkg import identity as side

    assert side.FINGERPRINTED_ORIGIN_PROVIDERS == server.FINGERPRINTED_ORIGIN_PROVIDERS
    for value in ("sk-abc", "  sk-abc  ", "x" * 200, "", None):
        assert side.credential_fingerprint(value) == server.credential_fingerprint(value)
    assert side.keyed_credential_origin("env:K", "sk-abc") == server.keyed_credential_origin(
        "env:K", "sk-abc"
    )


# --- hosts.yml parsing -------------------------------------------------------

GH_WITH_GHE_AND_USERS = """\
github.example.corp:
    oauth_token: ghe-secret
    user: corp-user
github.com:
    users:
        alice:
            oauth_token: gho_alice
        bob:
            oauth_token: gho_bob
    git_protocol: https
    oauth_token: gho_alice
    user: alice
"""


def test_parse_simple_yaml_nests_by_indent():
    data = sidecar.parse_simple_yaml(GH_WITH_GHE_AND_USERS)
    assert data["github.example.corp"] == {"oauth_token": "ghe-secret", "user": "corp-user"}
    assert data["github.com"]["users"]["bob"] == {"oauth_token": "gho_bob"}
    assert data["github.com"]["oauth_token"] == "gho_alice"


def test_github_token_ignores_other_hosts_and_other_users():
    """The old flat scan kept the *last* oauth_token in the file: a GHE host or a
    second user's token could stand in for github.com's."""
    data = sidecar.parse_simple_yaml(GH_WITH_GHE_AND_USERS)
    assert sidecar.GenericCollector.get_nested(data, "github.com.oauth_token") == "gho_alice"


def test_github_token_absent_when_gh_keeps_it_in_the_keyring():
    data = sidecar.parse_simple_yaml("github.com:\n    user: alice\n    git_protocol: https\n")
    assert sidecar.GenericCollector.get_nested(data, "github.com.oauth_token") is None


def test_parse_simple_yaml_handles_quotes_comments_and_lists():
    data = sidecar.parse_simple_yaml(
        "# a comment\n"
        "---\n"
        "github.com:\n"
        "  oauth_token: 'quoted'  \n"
        '  user: "dq"\n'
        "  note: plain # trailing comment\n"
        "  scopes:\n"
        "    - repo\n"
        "    - read:org\n"
        "  url: https://github.com/x\n"
    )
    host = data["github.com"]
    assert host["oauth_token"] == "quoted"
    assert host["user"] == "dq"
    assert host["note"] == "plain"
    assert sidecar.parse_simple_yaml("a: \"x # y\" # c\nb: 'it''s'")["a"] == "x # y"
    assert host["url"] == "https://github.com/x"


def test_get_nested_still_walks_plain_paths_and_fallbacks():
    data = {"token": {"access_token": "a"}, "claudeAiOauth": {"accessToken": "c"}}
    get = sidecar.GenericCollector.get_nested
    assert get(data, "token.access_token") == "a"
    assert get(data, "missing.path|claudeAiOauth.accessToken") == "c"
    assert get(data, "token.nope") is None
    assert get(data, ["token", "access_token"]) == "a"
    assert get({"a.b": {"c": "d"}}, "a.b.c") == "d"


def test_collect_provider_sends_the_github_com_token_from_a_real_hosts_file(tmp_path, monkeypatch):
    """End to end through the generated rule: the github.com token comes out, never the
    GHE host's, and the login is not sent as an account id."""
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(GH_WITH_GHE_AND_USERS, encoding="utf-8")
    config = copy.deepcopy(sidecar.__REGISTRY__["providers"]["github"])
    for rule in config["rules"]:
        if rule["type"] == "file":
            rule["paths"] = [str(hosts)]
    config["rules"] = [r for r in config["rules"] if r["type"] == "file"]

    results, _blocked = sidecar.GenericCollector.collect_provider("github", config)

    cards = [c for c in results if c.get("remaining") == "Token"]
    metadata = [c.get("metadata", {}) for c in cards]
    assert any(m.get("api_key") == "gho_alice" for m in metadata), results
    assert not any(m.get("api_key") == "ghe-secret" for m in metadata)
    assert not any(m.get("account_id") == "alice" for m in metadata)
