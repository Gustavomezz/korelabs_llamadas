"""
Tabla `conversations` del tenant — compartida con el bot de WhatsApp.

INSERT: persistir mensajes salientes del calls bot (e.g., el link de Meet
que mandamos por WhatsApp tras agendar).

SELECT: leer el historial WhatsApp del caller cuando recibimos una
llamada, para que el bot sepa si ya conoce a la persona y dar
seguimiento en vez de tratar la llamada como primera interacción.
"""
from typing import TypedDict

import asyncpg


class WhatsAppContext(TypedDict, total=False):
    name: str | None
    clinic_name: str | None
    qualified: bool
    total_messages: int
    recent_messages: list[dict]


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


async def get_recent_whatsapp_context(
    pool: asyncpg.Pool,
    wa_id: str,
    limit: int = 15,
) -> WhatsAppContext | None:
    """
    Lee el historial WhatsApp del caller. Devuelve None si nunca hubo
    conversación por WA con este número (caller "frío"). Devuelve dict
    con nombre, datos de calificación y últimos N mensajes si sí hubo.

    Filtra contenido vacío (mensajes de media sin caption, transcripciones
    fallidas, etc.) para que el modelo no vea ruido.
    """
    if not wa_id:
        return None

    async with pool.acquire() as conn:
        # Contar mensajes para decidir si hay historial relevante.
        # Filtramos contenido vacío en el conteo también — un solo "Hola"
        # de hace 3 meses sin respuesta sigue siendo historial mínimo,
        # pero ruido puro no.
        count_row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM conversations
            WHERE wa_id = $1
              AND content IS NOT NULL
              AND TRIM(content) != ''
            """,
            wa_id,
        )
        total = (count_row["cnt"] if count_row else 0) or 0
        if total == 0:
            return None

        # Datos del contacto (nombre del perfil WA, datos de calificación
        # que el bot WA llenó a través de las preguntas).
        contact_row = await conn.fetchrow(
            "SELECT name, clinic_name, qualified FROM contacts WHERE wa_id = $1",
            wa_id,
        )

        # Últimos N mensajes en orden cronológico.
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE wa_id = $1
              AND content IS NOT NULL
              AND TRIM(content) != ''
            ORDER BY created_at DESC
            LIMIT $2
            """,
            wa_id, limit,
        )

    recent = [
        {
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        for r in reversed(rows)  # invertir DESC → cronológico
    ]

    return {
        "name": (contact_row["name"] if contact_row else None),
        "clinic_name": (contact_row["clinic_name"] if contact_row else None),
        "qualified": bool(contact_row["qualified"]) if contact_row else False,
        "total_messages": total,
        "recent_messages": recent,
    }
