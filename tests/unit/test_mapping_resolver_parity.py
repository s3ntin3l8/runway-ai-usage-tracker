"""One table of mapping-key cases run against BOTH resolvers.

The sidecar (``GenericCollector.get_nested``) and the server
(``CredentialProvider._resolve_mapping_value``) interpret the same registry mapping
keys, so they must agree on every case, including the dotted-key backtracking one
that used to differ.
"""

from __future__ import annotations

import pytest

from app.services.credential_provider import CredentialProvider
from scripts import sidecar

RESOLVERS = {
    "sidecar": sidecar.GenericCollector.get_nested,
    "server": CredentialProvider._resolve_mapping_value,
}

APPS = {
    "github.com:Iv1.b507a08c87ecfe98": {"user": "me", "oauth_token": "tok-iv1"},
    "github.com:Iv23ctfURkiMfJ4xr5mv": {"user": "me", "oauth_token": "tok-iv23"},
    "foo.ghe.com:Iv1.b507a08c87ecfe98": {"oauth_token": "tok-ghe"},
}

# The MiniMax Code desktop app's auth store: one record per (buildEnv, region),
# keyed by "<service> <account-hash>". The registry rule reaches the token with
# a `records.*` wildcard, which has to span the dots AND the space in that key.
MINIMAX_CODE_AUTH = {
    "schemaVersion": 1,
    "records": {
        "com.minimax.mcode.oauth.prod.en whmxAo4Y9iIPa1gPWcGzY9bdtkLxQaMy3kWpwXHjJjA": {
            "schemaVersion": 1,
            "accessToken": "mmoat_x",
            "refreshToken": "mmort_x",
            "clientId": "mcode-public",
            "expiresAtMs": 1792518384753,
        }
    },
}

# Two logins in one store: the stale China record sorts before the live
# English one, so a resolver that took the first match would hand out a dead
# token. The live record has to win — and both the token and the expiry have
# to come from THAT record, not from two different ones.
MINIMAX_CODE_AUTH_TWO_RECORDS = {
    "schemaVersion": 1,
    "records": {
        "com.minimax.mcode.oauth.prod.cn deadbeef": {
            "schemaVersion": 1,
            "accessToken": "mmoat_dead",
            "refreshToken": "mmort_dead",
            "clientId": "mcode-public",
            "expiresAtMs": 1_600_000_000_000,  # long expired
        },
        "com.minimax.mcode.oauth.prod.en cafebabe": {
            "schemaVersion": 1,
            "accessToken": "mmoat_live",
            "refreshToken": "mmort_live",
            "clientId": "mcode-public",
            "expiresAtMs": 4_102_444_800_000,  # year 2100
        },
    },
}

CASES = [
    # id, data, path, expected
    ("plain nested", {"a": {"b": 1}}, "a.b", 1),
    ("missing", {"a": {"b": 1}}, "a.c", None),
    (
        "dotted key backtracking: the empty dotted key must not shadow the nested one",
        {"a.b": {}, "a": {"b": {"c": 1}}},
        "a.b.c",
        1,
    ),
    ("dotted key", {"github.com": {"oauth_token": "x"}}, "github.com.oauth_token", "x"),
    ("fallback chain, first truthy", {"b": 2}, "a|b", 2),
    ("fallback skips falsy", {"a": "", "b": 2}, "a|b", 2),
    ("literal final falsy key is returned as-is", {"a": ""}, "a", ""),
    ("wildcard hit spans dots and colons", APPS, "github.com:*.oauth_token", "tok-iv1"),
    (
        "wildcard order is sorted, not file order",
        {"k-b": {"t": "B"}, "k-a": {"t": "A"}},
        "k-*.t",
        "A",
    ),
    ("wildcard no hit", APPS, "bitbucket.org:*.oauth_token", None),
    ("wildcard does not reach into other hosts", APPS, "github.com:*.missing", None),
    (
        "wildcard skips a falsy match and takes the next",
        {"k-a": {"t": ""}, "k-b": {"t": "B"}},
        "k-*.t",
        "B",
    ),
    (
        "wildcard under a nested key",
        {"auth": {"https://github.com:me": {"token": "T"}}},
        "auth.https://github.com:*.token",
        "T",
    ),
    (
        "wildcard as the last segment returns the entry value",
        {"copilotTokens": {"https://github.com:me": "STR"}},
        "copilotTokens.https://github.com:*",
        "STR",
    ),
    (
        "wildcard last segment may return a dict (callers must not map it)",
        {"a": {"k1": {"x": 1}}},
        "a.k*",
        {"x": 1},
    ),
    (
        "alternatives combine with wildcards, literals first",
        APPS,
        "github.com:Iv23ctfURkiMfJ4xr5mv.oauth_token|github.com:*.oauth_token",
        "tok-iv23",
    ),
    (
        "the catch-all picks up an unknown client id",
        {"github.com:NewApp": {"oauth_token": "NEW"}},
        "github.com:Iv1.x.oauth_token|github.com:*.oauth_token",
        "NEW",
    ),
    ("question mark and bracket are literal", {"a?": {"t": 1}, "ab": {"t": 2}}, "a?.t", 1),
    ("star matches the empty string", {"k": {"t": 1}}, "k*.t", 1),
    ("non-dict data", "text", "a.b", None),
    ("empty dict", {}, "a", None),
    ("scalar in the middle of a path", {"a": 5}, "a.b", None),
    # MiniMax Code's auth store — the record key carries dots AND a space.
    (
        "minimax code: wildcard record key spans dots and a space",
        MINIMAX_CODE_AUTH,
        "records.*.accessToken",
        "mmoat_x",
    ),
    (
        "minimax code: wildcard record key reaches the refresh token",
        MINIMAX_CODE_AUTH,
        "records.*.refreshToken",
        "mmort_x",
    ),
    (
        "minimax code: wildcard record key reaches the expiry",
        MINIMAX_CODE_AUTH,
        "records.*.expiresAtMs",
        1792518384753,
    ),
    # Two logins in one store: the unexpired record wins over the dead one
    # that sorts first.
    (
        "minimax code: the unexpired record wins over the dead one that sorts first",
        MINIMAX_CODE_AUTH_TWO_RECORDS,
        "records.*.accessToken",
        "mmoat_live",
    ),
    (
        "minimax code: the expiry comes from the same record as the token",
        MINIMAX_CODE_AUTH_TWO_RECORDS,
        "records.*.expiresAtMs",
        4_102_444_800_000,
    ),
    (
        "minimax code: the refresh token comes from the same record too",
        MINIMAX_CODE_AUTH_TWO_RECORDS,
        "records.*.refreshToken",
        "mmort_live",
    ),
]


@pytest.mark.parametrize("name", RESOLVERS)
@pytest.mark.parametrize(("case_id", "data", "path", "expected"), CASES, ids=[c[0] for c in CASES])
def test_resolver_cases(name, case_id, data, path, expected):
    assert RESOLVERS[name](data, path) == expected


@pytest.mark.parametrize(("case_id", "data", "path", "_expected"), CASES, ids=[c[0] for c in CASES])
def test_sidecar_and_server_agree(case_id, data, path, _expected):
    assert sidecar.GenericCollector.get_nested(data, path) == (
        CredentialProvider._resolve_mapping_value(data, path)
    )


def test_key_glob_helpers_agree():
    for pattern in ("github.com:*", "a?b", "x*y*z", "plain", "*"):
        server = CredentialProvider._key_glob(pattern)
        side = sidecar._key_glob(pattern)
        for key in ("github.com:me", "a?b", "ab", "xyz", "x-y-z", "plain", ""):
            assert bool(server.fullmatch(key)) == bool(side.fullmatch(key)), (pattern, key)
