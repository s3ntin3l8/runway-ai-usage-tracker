"""Unit tests for multi-browser cookie extraction."""

import hashlib
import struct
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.browser_cookies import (
    decrypt_macos_cookie,
    decrypt_linux_cookie,
    SafariBinaryCookieParser,
    get_all_browser_cookies_paths,
)


def _make_cbc_cookie(password: str, plaintext: str, prefix: bytes = b"v10") -> bytes:
    """Create a Chrome-format v10/v11 CBC-encrypted cookie for testing."""
    raw = plaintext.encode("utf-8")
    pad_len = 16 - (len(raw) % 16)
    padded = raw + bytes([pad_len] * pad_len)
    key = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return prefix + enc.update(padded) + enc.finalize()


class TestDecryptMacosCookie:
    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear the keychain cache before each test to prevent cross-test pollution."""
        from app.core.keychain import clear_keychain_cache
        clear_keychain_cache()
        yield
        clear_keychain_cache()

    def test_decrypts_v10_cookie_correctly(self):
        password = "test_chrome_password"
        plaintext = "session_token_abc"
        encrypted = _make_cbc_cookie(password, plaintext, b"v10")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=password + "\n")
            result = decrypt_macos_cookie(encrypted)

        assert result == plaintext

    def test_decrypts_v10_edge_cookie_correctly(self):
        password = "test_edge_password"
        plaintext = "edge_session_123"
        encrypted = _make_cbc_cookie(password, plaintext, b"v10")

        with patch("subprocess.run") as mock_run:
            with patch("app.core.keychain._record_denial") as mock_deny:
                # decrypt_macos_cookie calls get_keychain_secret twice (Chrome, then Edge)
                # each get_keychain_secret can call subprocess.run twice (-w, then -g)
                # 1. Chrome -w fails
                # 2. Chrome -g fails
                # 3. Edge -w succeeds
                mock_run.side_effect = [
                    MagicMock(returncode=1, stdout="", stderr="error"),
                    MagicMock(returncode=1, stdout="", stderr="error"),
                    MagicMock(returncode=0, stdout=password + "\n", stderr=""),
                ]
                result = decrypt_macos_cookie(encrypted)

        assert result == plaintext


class TestSafariParser:
    def test_parses_binary_cookies_header(self):
        """Test the Safari binary cookie parser with a mock file."""
        # Signature, 1 page, page size 100
        mock_data = b"cook" + struct.pack(">I", 1) + struct.pack(">I", 32)
        # Page data: signature, num cookies=1, offset[0]=12
        page_data = b"\x00\x00\x01\x00" + struct.pack("<I", 1) + struct.pack("<I", 12)
        # Cookie data starts at offset 12. Strings start at 12 + 32 = 44 relative to page start.
        cookie_data = (
            b"A" * 16
            + struct.pack("<I", 32)
            + struct.pack("<I", 44)
            + struct.pack("<I", 52)
            + struct.pack("<I", 54)
            + b"example.com\x00"
            + b"session\x00"
            + b"/\x00"
            + b"xyz123\x00"
        )

        full_page = page_data + cookie_data

        with patch("builtins.open", MagicMock()) as mock_open:
            mock_open.return_value.__enter__.return_value.read.side_effect = [
                b"cook",
                struct.pack(">I", 1),
                struct.pack(">I", len(full_page)),
                full_page,
            ]
            cookies = SafariBinaryCookieParser.parse_file(
                Path("/tmp/mock.binarycookies")
            )

        assert len(cookies) == 1
        assert cookies[0]["domain"] == "example.com"
        assert cookies[0]["name"] == "session"
        assert cookies[0]["value"] == "xyz123"


class TestPathDiscovery:
    @patch("platform.system")
    @patch("pathlib.Path.home")
    def test_finds_edge_paths(self, mock_home, mock_system):
        mock_system.return_value = "Darwin"
        mock_home.return_value = Path("/Users/test")

        with patch.object(Path, "exists", autospec=True) as mock_exists:

            def exists_side_effect(path_instance):
                return "Microsoft Edge" in str(path_instance)

            mock_exists.side_effect = exists_side_effect

            paths = get_all_browser_cookies_paths()
            edge_paths = [p for p in paths if p["browser"] == "Edge"]
            assert len(edge_paths) > 0
            assert any("Microsoft Edge" in str(p["path"]) for p in edge_paths)

    @patch("platform.system")
    @patch("pathlib.Path.glob")
    @patch("pathlib.Path.home")
    def test_finds_firefox_paths(self, mock_home, mock_glob, mock_system):
        mock_system.return_value = "Linux"
        mock_home.return_value = Path("/home/test")
        mock_glob.return_value = [
            Path("/home/test/.mozilla/firefox/abc.default-release")
        ]

        with patch.object(Path, "exists", autospec=True) as mock_exists:

            def exists_side_effect(path_instance):
                return "firefox" in str(path_instance).lower()

            mock_exists.side_effect = exists_side_effect

            paths = get_all_browser_cookies_paths()
            ff_paths = [p for p in paths if p["browser"] == "Firefox"]
            assert len(ff_paths) >= 1
            assert "cookies.sqlite" in str(ff_paths[0]["path"])
