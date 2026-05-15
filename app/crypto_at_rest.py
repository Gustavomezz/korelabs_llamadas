"""Cifrado at-rest compatible con el bot de WhatsApp.

Los tokens de Google viven en la BD del tenant, compartida por WhatsApp y
llamadas. WhatsApp los guarda con prefijo `enc:v1:` usando Fernet; llamadas
debe leer y escribir en el mismo formato para no mandar ciphertext a Google.
"""
from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import logger, settings

_PREFIX = "enc:v1:"


def _fernet() -> Optional[Fernet]:
    if not settings.tenant_db_encryption_key:
        return None
    return Fernet(settings.tenant_db_encryption_key.encode())


def encrypt_at_rest(plain: Optional[str]) -> Optional[str]:
    if plain is None or plain == "":
        return plain
    if plain.startswith(_PREFIX):
        return plain
    f = _fernet()
    if f is None:
        logger.warning("encrypt_at_rest without TENANT_DB_ENCRYPTION_KEY; storing plaintext")
        return plain
    token = f.encrypt(plain.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_at_rest(stored: Optional[str]) -> Optional[str]:
    if stored is None or stored == "":
        return stored
    if not stored.startswith(_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        raise RuntimeError("TENANT_DB_ENCRYPTION_KEY not configured for encrypted tenant value")
    token = stored[len(_PREFIX):]
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("invalid TENANT_DB_ENCRYPTION_KEY for encrypted tenant value") from exc
