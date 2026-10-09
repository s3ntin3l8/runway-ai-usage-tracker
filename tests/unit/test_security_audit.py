import time
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

from app.core.encryption import EncryptionError, EncryptionService
from app.core.security import validate_ingest_auth, verify_config_signature
from app.services.webhooks import WebhookURLError, validate_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/webhook",
        "https://127.0.0.1/api/webhooks/a/b",
        "https://internal.example.test/webhook",
        "http://hooks.slack.com/services/a/b/c",
        "https://discord.com.attacker.test/api/webhooks/a/b",
        "https://discord.com:8443/api/webhooks/a/b",
        "https://user:pass@discord.com/api/webhooks/a/b",  # pragma: allowlist secret — synthetic regression fixture
        "https://hooks.slack.com/other",
        "https://discord.com/api/webhooks/a/b\n",
    ],
)
def test_webhook_only_accepts_provider_endpoints(url):
    with pytest.raises(WebhookURLError):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com/services/T/B/token",
        "https://discord.com/api/webhooks/123/token",
        "https://discordapp.com/api/webhooks/123/token",
    ],
)
def test_supported_webhook_urls(url):
    validate_webhook_url(url)


def test_configured_encryption_never_falls_back_to_plaintext():
    service = EncryptionService(Fernet.generate_key().decode())
    service._fernet = MagicMock()
    service._fernet.encrypt.side_effect = RuntimeError("failure")
    with pytest.raises(EncryptionError):
        service.encrypt_string("secret")


@pytest.mark.parametrize("length", [None, "1", str(10 * 1024 * 1024)])
async def test_ingest_body_cap_stops_receiving(monkeypatch, length):
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", "safe-ingest-secret")
    received = 0

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * (1024 * 1024), "more_body": True}

    headers = [(b"content-length", length.encode())] if length else []
    request = Request({"type": "http", "headers": headers}, receive)
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_auth(request, "invalid", str(time.time()))
    assert exc.value.status_code == 413
    assert received == (0 if length and int(length) > 8 * 1024 * 1024 else 9)


@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf"])
async def test_nonfinite_hmac_timestamps_rejected(monkeypatch, timestamp):
    monkeypatch.setattr("app.core.config.settings.INGEST_API_KEY", "safe-ingest-secret")
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-signature", b"invalid"),
                (b"x-timestamp", timestamp.encode()),
            ],
        }
    )
    with pytest.raises(HTTPException) as exc:
        await validate_ingest_auth(request, "invalid", timestamp)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        verify_config_signature(request)
    assert exc.value.status_code == 401
