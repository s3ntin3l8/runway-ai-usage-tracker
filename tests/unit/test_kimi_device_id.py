"""Kimi's device ID follows its CLI bearer, never an unrelated credential."""

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.kimi_device import normalize_device_id
from app.services.collectors.kimi_coding import KimiCodingCollector
from app.services.token_cache import TokenCache
from scripts import sidecar


def _jwt(exp):
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"e30.{encoded}.sig"


@pytest.mark.parametrize("value", [None, "", " \n", "bad\x00id", "bad\tid", "café", "x" * 257])
def test_device_id_rejects_invalid_values(value):
    assert normalize_device_id(value) is None


def test_sidecar_reads_device_read_only_and_forwards_it_only_with_cli_tokens(tmp_path, monkeypatch):
    root = tmp_path / "kimi-code"
    (root / "credentials").mkdir(parents=True)
    device = root / "device_id"
    device.write_text("opaque CLI device\n")
    credential = root / "credentials" / "kimi-code-test.json"
    credential.write_text(
        json.dumps(
            {
                "access_token": "cli-test",
                "expires_at": time.time() + 3600,
                "account": "work@example.com",
            }
        )
    )
    config = {
        "rules": [
            {
                "type": "file",
                "paths": [str(credential)],
                "mapping": {
                    "access_token": "cli_access_token",
                    "expires_at": "cli_expires_at",
                    "account": "account_id",
                },
            }
        ]
    }
    cards, _ = sidecar.GenericCollector.collect_provider("kimi_coding", config)
    assert cards[0]["metadata"]["cli_device_id"] == "opaque CLI device"
    assert device.read_text() == "opaque CLI device\n"
    device.write_bytes(b"bad\x00device")
    cards, _ = sidecar.GenericCollector.collect_provider("kimi_coding", config)
    assert "cli_device_id" not in cards[0]["metadata"]
    device.unlink()
    cards, _ = sidecar.GenericCollector.collect_provider("kimi_coding", config)
    assert "cli_device_id" not in cards[0]["metadata"]
    assert not device.exists()
    monkeypatch.setattr(
        sidecar, "read_kimi_cli_device_id", lambda *_: pytest.fail("API key read device file")
    )
    config["rules"][0]["mapping"] = {"access_token": "api_key", "account": "account_id"}
    cards, _ = sidecar.GenericCollector.collect_provider("kimi_coding", config)
    assert "cli_device_id" not in cards[0]["metadata"]


@pytest.mark.parametrize("contents", [b"x" * 259, b"\xff", b"bad\nid"])
def test_sidecar_omits_invalid_device_file(tmp_path, contents):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (tmp_path / "device_id").write_bytes(contents)
    assert sidecar.read_kimi_cli_device_id(credentials / "token.json") is None


@pytest.mark.asyncio
async def test_token_updates_keep_source_bundles_atomic_and_clear_absent_device():
    cache = TokenCache()
    future = time.time() + 3600
    await cache.store(
        "kimi_coding",
        {"cli_access_token": _jwt(future), "cli_expires_at": str(future), "cli_device_id": "first"},
        account_id="work",
        source_id="a",
    )
    await cache.store(
        "kimi_coding",
        {
            "cli_access_token": _jwt(future + 60),
            "cli_expires_at": str(future + 60),
            "cli_device_id": "second",
        },
        account_id="work",
        source_id="b",
    )
    async with cache.using_source("kimi_coding", "work", "a"):
        assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "first"
    async with cache.using_source("kimi_coding", "work", "b"):
        assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "second"
    await cache.store(
        "kimi_coding",
        {"cli_access_token": _jwt(future + 120), "cli_expires_at": str(future + 120)},
        account_id="work",
        source_id="b",
    )
    async with cache.using_source("kimi_coding", "work", "b"):
        assert "cli_device_id" not in await cache.get("kimi_coding", "work")
    assert "cli_device_id" not in await cache.get("kimi_coding", "work")
    await cache.store(
        "kimi_coding",
        {
            "cli_access_token": _jwt(future - 60),
            "cli_expires_at": str(future - 60),
            "cli_device_id": "stale",
        },
        account_id="work",
        source_id="b",
    )
    async with cache.using_source("kimi_coding", "work", "b"):
        assert "cli_device_id" not in await cache.get("kimi_coding", "work")
    assert "cli_device_id" not in await cache.get("kimi_coding", "work")


@pytest.mark.asyncio
async def test_concurrent_collections_use_their_pinned_bearer_and_device(monkeypatch):
    import app.services.collectors.kimi_coding as kimi

    cache = TokenCache()
    monkeypatch.setattr(kimi, "token_cache", cache)
    monkeypatch.setattr("app.services.token_cache.token_cache", cache)
    monkeypatch.setattr(kimi.settings, "KIMI_CODE_API_KEY", "")
    future = str(time.time() + 3600)
    for source, device in (("a", "device-a"), ("b", "device-b"), ("c", None)):
        tokens = {"cli_access_token": f"token-{source}", "cli_expires_at": future}
        if device:
            tokens["cli_device_id"] = device
        await cache.store("kimi_coding", tokens, account_id="work", source_id=source)
    calls = []

    async def request(_client, _method, _url, **kwargs):
        await asyncio.sleep(0)
        calls.append(kwargs["headers"])
        return httpx.Response(200, json={})

    monkeypatch.setattr(kimi, "http_request_with_retry", request)

    async def collect(source):
        async with cache.using_source("kimi_coding", "work", source):
            await KimiCodingCollector(account_id="work")._strategy_code_api(AsyncMock())

    await asyncio.gather(*(collect(source) for source in ("a", "b", "c")))
    headers = {item["Authorization"]: item for item in calls}
    assert headers["Bearer token-a"]["X-Msh-Device-Id"] == "device-a"
    assert headers["Bearer token-b"]["X-Msh-Device-Id"] == "device-b"
    fallback = KimiCodingCollector(account_id="work")._kimi_code_identity_headers()[
        "X-Msh-Device-Id"
    ]
    assert headers["Bearer token-c"]["X-Msh-Device-Id"] == fallback
    assert (
        KimiCodingCollector(account_id="work")._kimi_code_identity_headers("\x00bad")[
            "X-Msh-Device-Id"
        ]
        == fallback
    )


@pytest.mark.asyncio
async def test_promotion_and_retirement_keep_cli_identity_with_bearer():
    cache = TokenCache()
    future = time.time() + 3600
    for account, offset, device in (("pending", 0, "old-device"), ("work", 60, "new-device")):
        await cache.store(
            "kimi_coding",
            {
                "cli_access_token": _jwt(future + offset),
                "cli_expires_at": str(future + offset),
                "cli_device_id": device,
            },
            account_id=account,
            source_id="source",
        )
    assert await cache.move_source("kimi_coding", "pending", "work", "source")
    async with cache.using_source("kimi_coding", "work", "source"):
        tokens = await cache.get("kimi_coding", "work")
        assert tokens["cli_device_id"] == "new-device"
        assert tokens["cli_access_token"] == _jwt(future + 60)
    await cache.store(
        "kimi_coding",
        {"api_key": "independent-key"},  # pragma: allowlist secret
        account_id="work",
    )
    await cache.remove_source("kimi_coding", "work", "source", retire_matching_oauth=True)
    assert await cache.get("kimi_coding", "work") == {
        "api_key": "independent-key"  # pragma: allowlist secret
    }


@pytest.mark.asyncio
async def test_api_key_requests_never_include_cli_device_headers(monkeypatch):
    import app.services.collectors.kimi_coding as kimi

    collector = KimiCodingCollector(account_id="work")
    monkeypatch.setattr(
        collector,
        "_resolve_code_bearer",
        AsyncMock(return_value=("api-test", "config", False, "device-ignored")),
    )
    request = AsyncMock(return_value=httpx.Response(200, json={}))
    monkeypatch.setattr(kimi, "http_request_with_retry", request)
    await collector._strategy_code_api(AsyncMock())
    assert not any(key.startswith("X-Msh-") for key in request.call_args.kwargs["headers"])


@pytest.mark.asyncio
async def test_opaque_cli_expiry_prevents_stale_device_replacement():
    cache = TokenCache()
    future = time.time() + 3600
    for token, expiry, device in (
        ("fresh", future, "correct"),
        ("stale", future - 60, "wrong"),
        ("expired", time.time() - 60, "wrong"),
    ):
        await cache.store(
            "kimi_coding",
            {"cli_access_token": token, "cli_expires_at": str(expiry), "cli_device_id": device},
            account_id="work",
            source_id="a",
        )
    async with cache.using_source("kimi_coding", "work", "a"):
        assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "correct"
        assert (await cache.get("kimi_coding", "work"))["cli_access_token"] == "fresh"
    assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "correct"


@pytest.mark.asyncio
async def test_pending_source_promotion_cannot_downgrade_another_live_cli_bundle():
    cache = TokenCache()
    future = time.time() + 3600
    await cache.store(
        "kimi_coding",
        {"cli_access_token": "old", "cli_expires_at": str(future), "cli_device_id": "device-old"},
        account_id="pending",
        source_id="a",
        source_metadata={"identity_pending": True},
    )
    await cache.store(
        "kimi_coding",
        {
            "cli_access_token": "fresh",
            "cli_expires_at": str(future + 3600),
            "cli_device_id": "device-fresh",
        },
        account_id="work",
        source_id="b",
    )
    assert await cache.move_source("kimi_coding", "pending", "work", "a")
    assert (await cache.get("kimi_coding", "work"))["cli_access_token"] == "fresh"
    assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "device-fresh"
    async with cache.using_source("kimi_coding", "work", "a"):
        assert (await cache.get("kimi_coding", "work"))["cli_device_id"] == "device-old"
