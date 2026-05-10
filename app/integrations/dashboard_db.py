"""Acceso a la Dashboard DB y descifrado Fernet de los database_url de tenants."""
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@dataclass(frozen=True)
class TenantRecord:
    id: int
    slug: str
    display_name: Optional[str]
    database_url: str
    voice_phone_number_e164: Optional[str]
    voice_enabled: bool


def _fernet() -> Fernet:
    if not settings.tenant_db_encryption_key:
        raise RuntimeError("TENANT_DB_ENCRYPTION_KEY not configured")
    return Fernet(settings.tenant_db_encryption_key.encode())


def decrypt_database_url(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("invalid TENANT_DB_ENCRYPTION_KEY for stored token") from exc


def encrypt_database_url(url: str) -> str:
    return _fernet().encrypt(url.encode()).decode()


async def fetch_tenant_by_voice_number(pool, e164: str) -> Optional[TenantRecord]:
    row = await pool.fetchrow(
        """
        SELECT id, slug, display_name, database_url_encrypted,
               voice_phone_number_e164, voice_enabled
        FROM tenants
        WHERE voice_phone_number_e164 = $1
        LIMIT 1
        """,
        e164,
    )
    if row is None:
        return None
    return TenantRecord(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        database_url=decrypt_database_url(row["database_url_encrypted"]),
        voice_phone_number_e164=row["voice_phone_number_e164"],
        voice_enabled=row["voice_enabled"],
    )


async def fetch_tenant_by_slug(pool, slug: str) -> Optional[TenantRecord]:
    row = await pool.fetchrow(
        """
        SELECT id, slug, display_name, database_url_encrypted,
               voice_phone_number_e164, voice_enabled
        FROM tenants
        WHERE slug = $1
        LIMIT 1
        """,
        slug,
    )
    if row is None:
        return None
    return TenantRecord(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        database_url=decrypt_database_url(row["database_url_encrypted"]),
        voice_phone_number_e164=row["voice_phone_number_e164"],
        voice_enabled=row["voice_enabled"],
    )


async def assign_voice_number(pool, slug: str, e164: str, enabled: bool = True) -> bool:
    """Asigna número Twilio a un tenant existente. Devuelve True si actualizó alguna fila."""
    result = await pool.execute(
        """
        UPDATE tenants
        SET voice_phone_number_e164 = $1,
            voice_enabled = $2
        WHERE slug = $3
        """,
        e164,
        enabled,
        slug,
    )
    return result.endswith(" 1")
