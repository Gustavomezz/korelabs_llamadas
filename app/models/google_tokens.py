"""CRUD de tokens OAuth de Google.

La tabla vive en la BD del tenant y también la usa el bot de WhatsApp. Por
eso `access_token` y `refresh_token` se cifran/descifran con el mismo formato
`enc:v1:` antes de tocar Google.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from app.crypto_at_rest import decrypt_at_rest, encrypt_at_rest


async def save_google_tokens(
    pool: asyncpg.Pool,
    *,
    owner_email: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO google_tokens (owner_email, access_token, refresh_token, expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (owner_email) DO UPDATE
            SET access_token = $2, refresh_token = $3, expires_at = $4
            """,
            owner_email, encrypt_at_rest(access_token), encrypt_at_rest(refresh_token),
            expires_at.replace(tzinfo=None),
        )


async def get_latest_token(pool: asyncpg.Pool) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT owner_email, access_token, refresh_token, expires_at
            FROM google_tokens ORDER BY created_at DESC LIMIT 1
            """
        )
    if not row:
        return None
    return {
        "owner_email": row["owner_email"],
        "access_token": decrypt_at_rest(row["access_token"]),
        "refresh_token": decrypt_at_rest(row["refresh_token"]),
        "expires_at": row["expires_at"],
    }
