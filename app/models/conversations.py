"""
Persistencia mínima en la tabla `conversations` del tenant (compartida
con el bot de WhatsApp). El dashboard escucha cambios en esta tabla via
trigger NOTIFY, así que cualquier mensaje guardado aquí aparece en el
historial del lead automáticamente.

Solo necesitamos INSERT — la lectura/UI vive en el bot WA y el dashboard.
"""
import asyncpg


async def save_outgoing_wa_message(
    pool: asyncpg.Pool,
    *,
    wa_id: str,
    content: str,
    wa_message_id: str | None = None,
) -> None:
    """Inserta un mensaje saliente (role='assistant', source='bot') en
    conversations. El trigger del bot WA actualiza contacts (last_message,
    msg_count, etc.) automáticamente."""
    delivery_status = "sent" if wa_message_id else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversations (
                wa_id, role, content, source, wa_message_id, delivery_status
            )
            VALUES ($1, 'assistant', $2, 'bot', $3, $4)
            """,
            wa_id, content, wa_message_id, delivery_status,
        )
