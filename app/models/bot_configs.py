"""Lectura del voice_prompt activo del tenant."""
from typing import Optional

import asyncpg

from app.ai.default_voice_prompt import DEFAULT_VOICE_PROMPT


async def get_active_voice_prompt(pool: asyncpg.Pool) -> str:
    """
    Devuelve el voice_prompt del bot_config activo del tenant. Si no hay ninguno
    activo o el campo está NULL, regresa el prompt por defecto. Nunca regresa
    string vacía.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT voice_prompt FROM bot_configs WHERE is_active = TRUE ORDER BY id LIMIT 1"
        )
    if row is None:
        return DEFAULT_VOICE_PROMPT
    return (row["voice_prompt"] or "").strip() or DEFAULT_VOICE_PROMPT
