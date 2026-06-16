import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.encryption import EncryptionService


def test_encryption_roundtrip():
    # Create a test key
    key = Fernet.generate_key().decode()
    service = EncryptionService(key=key)

    original = "secret-token-123"
    encrypted = service.encrypt_string(original)
    assert encrypted != original

    decrypted = service.decrypt_string(encrypted)
    assert decrypted == original


def test_encryption_json_roundtrip():
    key = Fernet.generate_key().decode()
    service = EncryptionService(key=key)

    original = {"key": "value", "nested": [1, 2, 3]}
    encrypted = service.encrypt_json(original)

    decrypted = service.decrypt_json(encrypted)
    assert decrypted == original


def test_encryption_plaintext_fallback(monkeypatch):
    # No key provided
    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", None)
    service = EncryptionService(key=None)
    assert service.is_enabled is False

    original = "not-secret"
    encrypted = service.encrypt_string(original)
    assert encrypted == original

    decrypted = service.decrypt_string(encrypted)
    assert decrypted == original


def test_encryption_invalid_key_fails_fast():
    # A provided-but-malformed key must crash rather than silently downgrade to
    # plaintext — otherwise the operator believes secrets are encrypted.
    # (An absent key is the legitimate plaintext mode — see
    # test_encryption_plaintext_fallback above.)
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        EncryptionService(key="invalid-key")
