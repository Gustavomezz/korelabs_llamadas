"""CRUD de la tabla `calls` y upsert de `contacts` para contacto unificado."""
from typing import Optional

import asyncpg


async def upsert_contact(pool: asyncpg.Pool, wa_id: str) -> None:
    """Si el contacto no existe lo crea con datos mínimos. Si existe no toca nada."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO contacts (wa_id, first_contact, last_message)
            VALUES ($1, NOW(), NOW())
            ON CONFLICT (wa_id) DO NOTHING
            """,
            wa_id,
        )


async def create_call(
    pool: asyncpg.Pool,
    *,
    call_sid: str,
    caller_number: str,
    to_number: str,
    wa_id: Optional[str],
    metadata: Optional[dict] = None,
) -> int:
    """Crea fila en `calls` con status='ringing' y devuelve su id."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO calls (call_sid, caller_number, to_number, wa_id, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (call_sid) DO UPDATE
                SET caller_number = EXCLUDED.caller_number,
                    to_number = EXCLUDED.to_number,
                    wa_id = COALESCE(calls.wa_id, EXCLUDED.wa_id)
            RETURNING id
            """,
            call_sid,
            caller_number,
            to_number,
            wa_id,
            _json(metadata or {}),
        )


async def update_call_status(
    pool: asyncpg.Pool,
    *,
    call_sid: str,
    status: str,
    ended_reason: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    recording_url: Optional[str] = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE calls
            SET status = $2,
                answered_at = CASE
                    WHEN $2 = 'in-progress' AND answered_at IS NULL THEN NOW()
                    ELSE answered_at
                END,
                ended_at = CASE
                    WHEN $2 IN ('completed', 'failed', 'canceled', 'no-answer', 'busy')
                         AND ended_at IS NULL THEN NOW()
                    ELSE ended_at
                END,
                ended_reason = COALESCE($3, ended_reason),
                duration_seconds = COALESCE($4, duration_seconds),
                recording_url = COALESCE($5, recording_url)
            WHERE call_sid = $1
            """,
            call_sid,
            status,
            ended_reason,
            duration_seconds,
            recording_url,
        )


def _json(value: dict) -> str:
    import json
    return json.dumps(value, default=str)
