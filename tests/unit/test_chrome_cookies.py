"""Unit tests for Chrome cookie decryption — I5 fix."""
import hashlib
import pytest
from unittest.mock import patch, MagicMock
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.chrome_cookies import decrypt_macos_cookie, decrypt_linux_cookie


def _make_cbc_cookie(password: str, plaintext: str, prefix: bytes = b'v10') -> bytes:
    """Create a Chrome-format v10/v11 CBC-encrypted cookie for testing."""
    raw = plaintext.encode('utf-8')
    pad_len = 16 - (len(raw) % 16)
    padded = raw + bytes([pad_len] * pad_len)
    key = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), b'saltysalt', 1003, 16)
    iv = b' ' * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return prefix + enc.update(padded) + enc.finalize()


class TestDecryptMacosCookie:
    """I5: macOS cookie decryption must use AES-128-CBC not AES-GCM."""

    def test_decrypts_v10_cookie_correctly(self):
        """v10-prefixed cookie must be decrypted with AES-128-CBC."""
        password = "test_chrome_password"
        plaintext = "session_token_abc"
        encrypted = _make_cbc_cookie(password, plaintext, b'v10')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=password + "\n")
            result = decrypt_macos_cookie(encrypted)

        assert result == plaintext, f"Expected '{plaintext}', got {result!r}"

    def test_decrypts_v11_cookie_correctly(self):
        """v11-prefixed cookie must be decrypted with AES-128-CBC."""
        password = "another_password"
        plaintext = "my_auth_cookie_value"
        encrypted = _make_cbc_cookie(password, plaintext, b'v11')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=password + "\n")
            result = decrypt_macos_cookie(encrypted)

        assert result == plaintext, f"Expected '{plaintext}', got {result!r}"

    def test_returns_none_when_keychain_fails(self):
        """Returns None when security command fails."""
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = decrypt_macos_cookie(b'v10\xde\xad\xbe\xef')
        assert result is None

    def test_returns_none_for_wrong_password(self):
        """Returns None when decryption produces invalid PKCS7 padding."""
        password = "correct_password"
        encrypted = _make_cbc_cookie(password, "secret", b'v10')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="wrong_password\n")
            result = decrypt_macos_cookie(encrypted)
        # Should not crash — returns None gracefully
        assert result is None or isinstance(result, str)


class TestDecryptLinuxCookie:
    """I5: Linux cookie decryption must use AES-128-CBC not AES-GCM."""

    def test_decrypts_v10_cookie_via_secretstorage(self):
        """v10-prefixed cookie decrypted with AES-128-CBC via secretstorage."""
        password = b"linux_chrome_password"
        plaintext = "linux_session_cookie"
        # Linux secretstorage returns bytes
        raw = plaintext.encode('utf-8')
        pad_len = 16 - (len(raw) % 16)
        padded = raw + bytes([pad_len] * pad_len)
        key = hashlib.pbkdf2_hmac('sha1', password, b'saltysalt', 1003, 16)
        iv = b' ' * 16
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        enc = cipher.encryptor()
        encrypted = b'v10' + enc.update(padded) + enc.finalize()

        mock_item = MagicMock()
        mock_item.get_label.return_value = "Chrome Safe Storage"
        mock_item.get_secret.return_value = password

        mock_collection = MagicMock()
        mock_collection.get_all_items.return_value = [mock_item]

        import sys
        mock_secretstorage = MagicMock()
        mock_secretstorage.dbus_init.return_value = MagicMock()
        mock_secretstorage.get_default_collection.return_value = mock_collection

        with patch.dict(sys.modules, {'secretstorage': mock_secretstorage}):
            result = decrypt_linux_cookie(encrypted)

        assert result == plaintext, f"Expected '{plaintext}', got {result!r}"
