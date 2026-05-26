import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


class DecryptionError(Exception):
    """Decryption of a value that looks like Fernet ciphertext failed.

    Distinct from "value was never encrypted in the first place" — that
    case still returns the value unchanged so legacy plaintext rows stay
    usable across migrations.
    """


# Fernet tokens in URL-safe base64 always start with this prefix (version
# byte 0x80 + 8-byte timestamp). If a stored value does NOT start with this,
# we treat it as legacy plaintext rather than as a decryption failure.
_FERNET_PREFIX = "gAAAAA"


class EncryptionService:
    def __init__(self, key: str | None = None):
        self._key = key or settings.DB_ENCRYPTION_KEY
        self._fernet: Fernet | None = None

        if self._key:
            try:
                # Fernet key must be 32 url-safe base64-encoded bytes
                self._fernet = Fernet(self._key.encode())
                logger.info("Encryption service initialized with provided key.")
            except Exception as e:
                logger.error(
                    f"Failed to initialize Fernet encryption: {e}. Check DB_ENCRYPTION_KEY format."
                )
        else:
            logger.warning("No DB_ENCRYPTION_KEY found. Database will store data in PLAINTEXT.")

    @property
    def is_enabled(self) -> bool:
        return self._fernet is not None

    def encrypt_string(self, plaintext: str) -> str:
        """Encrypt a string and return as string."""
        fernet = self._fernet
        if not fernet:
            return plaintext

        try:
            return fernet.encrypt(plaintext.encode()).decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return plaintext

    def decrypt_string(self, ciphertext: str) -> str:
        """Decrypt a string and return plaintext.

        Raises DecryptionError if the value LOOKS like Fernet ciphertext
        (starts with the Fernet prefix) but cannot be decrypted — that
        means the key changed or the data is corrupted, and silently
        handing back the ciphertext would let callers operate on garbage.

        Returns the value unchanged in two recoverable cases:
        - Encryption was never enabled on this service (no key configured).
        - The stored value is legacy plaintext written before encryption
          was turned on; we detect this by the absence of the Fernet prefix.
        """
        fernet = self._fernet
        if not fernet:
            return ciphertext

        try:
            return fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            if ciphertext.startswith(_FERNET_PREFIX):
                # Real failure: data was encrypted under a different key,
                # or has been tampered with. Loud failure is correct here.
                logger.error(
                    "Decryption failed for value with Fernet prefix — key mismatch or corruption."
                )
                raise DecryptionError("Fernet token decryption failed") from exc
            # Looks like legacy plaintext from before encryption was enabled.
            return ciphertext
        except Exception as exc:
            logger.error(f"Unexpected decryption error: {exc}")
            raise DecryptionError("Decryption failed") from exc

    def encrypt_json(self, data: Any) -> str:
        """Serialize and encrypt any JSON-serializable data."""
        plaintext = json.dumps(data)
        return self.encrypt_string(plaintext)

    def decrypt_json(self, ciphertext: str) -> Any:
        """Decrypt and deserialize JSON string.

        Propagates DecryptionError for Fernet-prefixed values that fail
        to decrypt. Returns {} only when decryption succeeds but the
        resulting plaintext is not valid JSON.
        """
        plaintext = self.decrypt_string(ciphertext)
        try:
            return json.loads(plaintext)
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON after decryption. Data might be corrupted.")
            return {}


# Global instance
encryption_service = EncryptionService()
