from cryptography.fernet import Fernet

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


def test_encryption_plaintext_fallback():
    # No key provided
    service = EncryptionService(key=None)
    assert service.is_enabled is False

    original = "not-secret"
    encrypted = service.encrypt_string(original)
    assert encrypted == original

    decrypted = service.decrypt_string(encrypted)
    assert decrypted == original


def test_encryption_invalid_key():
    # Invalid key format should log error and disable encryption
    service = EncryptionService(key="invalid-key")
    assert service.is_enabled is False

    original = "fallback"
    assert service.encrypt_string(original) == original
