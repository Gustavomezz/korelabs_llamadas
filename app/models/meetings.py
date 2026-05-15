"""Persistencia de reuniones agendadas durante la llamada."""
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from app.config import logger, settings


def _calendar_local_naive(iso_str: str) -> datetime:
    """Convierte un ISO aware a hora local del calendario sin tzinfo.

    La tabla `meetings.start_time` del dashboard es `timestamp without time
    zone`; el frontend la interpreta como hora local. Por eso debemos guardar
    15:00 México como `2026-05-15 15:00`, no como `2026-05-15 21:00` UTC.
    """
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt
    try:
        tz = ZoneInfo(settings.calendar_timezone)
    except ZoneInfoNotFoundError:
        logger.error("invalid CALENDAR_TIMEZONE=%s, storing UTC naive", settings.calendar_timezone)
        return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return dt.astimezone(tz).replace(tzinfo=None)


async def save_meeting(
    pool: asyncpg.Pool,
    *,
    wa_id: str,
    event_id: str,
    attendee_email: str,
    start_iso: str,
    end_iso: str,
    meet_link: str,
    attendee_name: str = "",
    clinic_name: str = "",
    source_channel: str = "voice",
    status: str = "scheduled",
) -> None:
    try:
        async with pool.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE meetings
                SET wa_id = COALESCE(NULLIF($2, ''), wa_id),
                    attendee_email = COALESCE(NULLIF($3, ''), attendee_email),
                    attendee_name = COALESCE(NULLIF($4, ''), attendee_name),
                    clinic_name = COALESCE(NULLIF($5, ''), clinic_name),
                    source_channel = COALESCE(NULLIF($6, ''), source_channel),
                    status = COALESCE(NULLIF($7, ''), status),
                    start_time = $8,
                    end_time = $9,
                    meet_link = COALESCE(NULLIF($10, ''), meet_link)
                WHERE event_id = $1
                """,
                event_id,
                wa_id,
                attendee_email,
                attendee_name,
                clinic_name,
                source_channel,
                status,
                _calendar_local_naive(start_iso),
                _calendar_local_naive(end_iso),
                meet_link,
            )
            if updated != "UPDATE 0":
                return

            await conn.execute(
                """
                INSERT INTO meetings (
                    wa_id, event_id, attendee_email, attendee_name, clinic_name,
                    source_channel, status, start_time, end_time, meet_link
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                wa_id, event_id, attendee_email, attendee_name, clinic_name,
                source_channel, status,
                _calendar_local_naive(start_iso),
                _calendar_local_naive(end_iso),
                meet_link,
            )
    except Exception:
        logger.exception("could not save meeting")


async def update_meeting_schedule(
    pool: asyncpg.Pool,
    *,
    event_id: str,
    wa_id: str,
    attendee_email: str,
    start_iso: str,
    end_iso: str,
    meet_link: str = "",
    attendee_name: str = "",
    clinic_name: str = "",
    status: str = "scheduled",
) -> None:
    """Actualiza la fila local cuando Google Calendar mantiene el event_id.

    Reagendar por PATCH no crea evento nuevo en Google; si no movemos la fila
    `meetings`, el dashboard sigue mostrando la hora/contacto anterior.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE meetings
                SET wa_id = COALESCE(NULLIF($2, ''), wa_id),
                    attendee_email = COALESCE(NULLIF($3, ''), attendee_email),
                    attendee_name = COALESCE(NULLIF($4, ''), attendee_name),
                    clinic_name = COALESCE(NULLIF($5, ''), clinic_name),
                    status = COALESCE(NULLIF($6, ''), status),
                    start_time = $7,
                    end_time = $8,
                    meet_link = COALESCE(NULLIF($9, ''), meet_link)
                WHERE event_id = $1
                """,
                event_id,
                wa_id,
                attendee_email,
                attendee_name,
                clinic_name,
                status,
                _calendar_local_naive(start_iso),
                _calendar_local_naive(end_iso),
                meet_link,
            )
    except Exception:
        logger.exception("could not update meeting schedule")


async def mark_meeting_cancelled(pool: asyncpg.Pool, *, event_id: str) -> None:
    if not event_id:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE meetings SET status = 'cancelled' WHERE event_id = $1",
                event_id,
            )
    except Exception:
        logger.exception("could not mark meeting cancelled")


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
