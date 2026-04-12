from cryptography.fernet import Fernet
from app.core.config import settings
import os
import logging
from typing import Optional, Any
import json

logger = logging.getLogger(__name__)

class EncryptionService:
    def __init__(self, key: Optional[str] = None):
        self._key = key or settings.DB_ENCRYPTION_KEY
        self._fernet: Optional[Fernet] = None
        
        if self._key:
            try:
                # Fernet key must be 32 url-safe base64-encoded bytes
                self._fernet = Fernet(self._key.encode())
                logger.info("Encryption service initialized with provided key.")
            except Exception as e:
                logger.error(f"Failed to initialize Fernet encryption: {e}. Check DB_ENCRYPTION_KEY format.")
        else:
            logger.warning("No DB_ENCRYPTION_KEY found. Database will store data in PLAINTEXT.")

    @property
    def is_enabled(self) -> bool:
        return self._fernet is not None

    def encrypt_string(self, plaintext: str) -> str:
        """Encrypt a string and return as string."""
        if not self.is_enabled:
            return plaintext
        
        try:
            return self._fernet.encrypt(plaintext.encode()).decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return plaintext

    def decrypt_string(self, ciphertext: str) -> str:
        """Decrypt a string and return plaintext."""
        if not self.is_enabled:
            return ciphertext
        
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}. Perhaps the key changed?")
            return ciphertext # Return as is (might be plaintext or corrupted)

    def encrypt_json(self, data: Any) -> str:
        """Serialize and encrypt any JSON-serializable data."""
        plaintext = json.dumps(data)
        return self.encrypt_string(plaintext)

    def decrypt_json(self, ciphertext: str) -> Any:
        """Decrypt and deserialize JSON string."""
        plaintext = self.decrypt_string(ciphertext)
        try:
            return json.loads(plaintext)
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON after decryption. Data might be corrupted or key mismatch.")
            return {}

# Global instance
encryption_service = EncryptionService()
