"""Persistencia de reuniones agendadas durante la llamada."""
from datetime import datetime

import asyncpg

from app.config import logger


async def save_meeting(
    pool: asyncpg.Pool,
    *,
    wa_id: str,
    event_id: str,
    attendee_email: str,
    start_iso: str,
    end_iso: str,
    meet_link: str,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO meetings (wa_id, event_id, attendee_email, start_time, end_time, meet_link)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                wa_id, event_id, attendee_email,
                datetime.fromisoformat(start_iso.replace("Z", "+00:00")).replace(tzinfo=None),
                datetime.fromisoformat(end_iso.replace("Z", "+00:00")).replace(tzinfo=None),
                meet_link,
            )
    except Exception:
        logger.exception("could not save meeting")


async def save_meeting_action(
    pool: asyncpg.Pool,
    *,
    wa_id: str,
    event_id: str,
    action: str,
    attendee_email: str = "",
    details: str = "",
) -> None:
    """action: 'create' | 'cancel' | 'reschedule'"""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO meeting_actions (wa_id, event_id, action, attendee_email, details)
                VALUES ($1, $2, $3, $4, $5)
                """,
                wa_id, event_id, action, attendee_email, details,
            )
    except Exception:
        logger.exception("failed to save meeting action")
