"""CRUD de tokens OAuth de Google. La lógica de refresh vive en integrations/google_calendar.py."""
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg


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
            owner_email, access_token, refresh_token,
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
        return dict(row) if row else None
