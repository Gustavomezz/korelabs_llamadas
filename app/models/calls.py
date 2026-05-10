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
        # Cast explícito de $2 a varchar: se usa tanto como valor para la columna
        # `status` (varchar) como para comparaciones con literales (text), y sin
        # cast asyncpg no puede decidir el tipo y aborta con AmbiguousParameterError.
        await conn.execute(
            """
            UPDATE calls
            SET status = $2::varchar,
                answered_at = CASE
                    WHEN $2::varchar = 'in-progress' AND answered_at IS NULL THEN NOW()
                    ELSE answered_at
                END,
                ended_at = CASE
                    WHEN $2::varchar IN ('completed', 'failed', 'canceled', 'no-answer', 'busy')
                         AND ended_at IS NULL THEN NOW()
                    ELSE ended_at
                END,
                ended_reason = COALESCE($3::varchar, ended_reason),
                duration_seconds = COALESCE($4::integer, duration_seconds),
                recording_url = COALESCE($5::text, recording_url)
            WHERE call_sid = $1
            """,
            call_sid,
            status,
            ended_reason,
            duration_seconds,
            recording_url,
        )


async def get_call_id_by_sid(pool: asyncpg.Pool, call_sid: str) -> Optional[int]:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM calls WHERE call_sid = $1", call_sid)


async def insert_transcript(
    pool: asyncpg.Pool,
    *,
    call_id: int,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_args: Optional[dict] = None,
    tool_result: Optional[dict] = None,
    audio_offset_ms: Optional[int] = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO call_transcripts
                (call_id, role, content, tool_name, tool_args, tool_result, audio_offset_ms)
            VALUES ($1, $2::varchar, $3, $4::varchar, $5::jsonb, $6::jsonb, $7::integer)
            """,
            call_id,
            role,
            content,
            tool_name,
            _json(tool_args) if tool_args is not None else None,
            _json(tool_result) if tool_result is not None else None,
            audio_offset_ms,
        )


def _json(value: dict) -> str:
    import json
    return json.dumps(value, default=str)
