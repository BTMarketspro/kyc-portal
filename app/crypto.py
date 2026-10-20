"""Envelope-free AES-256-GCM encryption for KYC data at rest.

Blob layout:  b"\x01" | key_id (8 bytes) | nonce (12 bytes) | ciphertext+tag

* key_id is the first 8 bytes of SHA-256(key), so data written with an old key can still
  be read after rotation (list old keys in KYC_ENCRYPTION_OLD_KEYS).
* Every blob is bound to its owner through AES-GCM associated data (e.g. "submission:42"),
  so a ciphertext copied onto another record fails to decrypt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

VERSION = b"\x01"


class CryptoError(RuntimeError):
    pass


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("Encryption key is not valid base64.") from exc
    if len(key) != 32:
        raise CryptoError("Encryption key must decode to exactly 32 bytes (AES-256).")
    return key


def _key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:8]


class Cipher:
    def __init__(self, primary: str, old: tuple[str, ...] = ()) -> None:
        if not primary:
            raise CryptoError(
                "KYC_ENCRYPTION_KEY is not set. Generate one with: python -m app.cli generate-key"
            )
        self._primary = _decode_key(primary)
        self._keys = {_key_id(self._primary): self._primary}
        for raw in old:
            k = _decode_key(raw)
            self._keys[_key_id(k)] = k
        # Separate sub-key for blind indexes, so index values reveal nothing about the AES key.
        self._index_key = hmac.new(self._primary, b"blind-index", hashlib.sha256).digest()

    def encrypt(self, data: bytes, context: str) -> bytes:
        nonce = os.urandom(12)
        ct = AESGCM(self._primary).encrypt(nonce, data, context.encode())
        return VERSION + _key_id(self._primary) + nonce + ct

    def decrypt(self, blob: bytes, context: str) -> bytes:
        if not blob or blob[:1] != VERSION:
            raise CryptoError("Unknown ciphertext format.")
        key = self._keys.get(blob[1:9])
        if key is None:
            raise CryptoError("Data was encrypted with a key that is not configured.")
        try:
            return AESGCM(key).decrypt(blob[9:21], blob[21:], context.encode())
        except Exception as exc:  # noqa: BLE001
            raise CryptoError("Decryption failed (wrong key or tampered data).") from exc

    def encrypt_json(self, value: Any, context: str) -> bytes:
        return self.encrypt(json.dumps(value, separators=(",", ":"), default=str).encode(), context)

    def decrypt_json(self, blob: bytes, context: str) -> Any:
        return json.loads(self.decrypt(blob, context))

    def blind_index(self, value: str | None) -> str | None:
        """Deterministic keyed hash for exact-match search on encrypted fields (e.g. email)."""
        if not value:
            return None
        norm = value.strip().lower().encode()
        return hmac.new(self._index_key, norm, hashlib.sha256).hexdigest()


_cipher: Cipher | None = None


def cipher() -> Cipher:
    global _cipher
    if _cipher is None:
        _cipher = Cipher(settings.encryption_key, settings.old_encryption_keys)
    return _cipher


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
