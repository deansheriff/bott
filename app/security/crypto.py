"""Encryption for agents' Chatwoot API tokens at rest (Fernet / AES-128-CBC + HMAC)."""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings

log = logging.getLogger(__name__)
_ephemeral_key: bytes | None = None


def _fernet() -> Fernet:
    global _ephemeral_key
    key = get_settings().token_encryption_key
    if key:
        return Fernet(key.encode())
    if get_settings().is_production:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    if _ephemeral_key is None:
        log.warning("crypto.ephemeral_key", extra={"note": "TOKEN_ENCRYPTION_KEY unset; dev-only key in use"})
        _ephemeral_key = Fernet.generate_key()
    return Fernet(_ephemeral_key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        log.warning("crypto.decrypt_failed")
        return None
