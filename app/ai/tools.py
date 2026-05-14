"""
Schemas + dispatcher de tools para OpenAI Realtime.

NOTA: Realtime usa un schema ligeramente distinto al de Chat Completions —
sin wrapper `function`. El bot de WhatsApp tiene `{"type":"function","function":{...}}`,
acá va `{"type":"function","name":"...","description":"...","parameters":{...}}`.
"""
import json
from dataclasses import dataclass

import asyncpg

from app.config import logger
from app.integrations.google_calendar import (
    book_meeting,
    cancel_meeting,
    get_available_slots,
    list_user_meetings,
    reschedule_meeting,
)


REALTIME_TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_available_slots",
        "description": (
            "Obtiene horarios disponibles en la agenda de Gustavo. "
            "Lunes a viernes, 9am-5:30pm hora México, slots de 30 min. "
            "Llama esta función SIEMPRE antes de proponer cualquier horario, "
            "y copia exactamente los start_iso/end_iso que devuelve. "
            "MODO DEFAULT (sin target_date): devuelve 3 horarios distribuidos "
            "en 3 días distintos (mañana, +3 días, +5 días) — úsalo para la "
            "propuesta inicial. "
            "MODO FECHA ESPECÍFICA (con target_date): devuelve TODOS los slots "
            "libres de ese día — úsalo cuando el usuario pida un día concreto "
            "('¿tienes el martes?', 'algo el 15 de mayo?'). Convierte el día "
            "que pidió a YYYY-MM-DD basándote en la fecha actual."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "Ventana de búsqueda en días (default 14). Solo aplica si NO se pasa target_date.",
                },
                "target_date": {
                    "type": "string",
                    "description": "Fecha específica en formato YYYY-MM-DD. Si se pasa, devuelve TODOS los slots libres de ese día.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "book_meeting",
        "description": (
            "Crea una reunión en Google Calendar con link de Google Meet. "
            "Usa SOLO un horario obtenido de get_available_slots. "
            "Google manda invitación al correo del attendee automáticamente. "
            "Adicionalmente, si el caller ya tiene historial WhatsApp, el "
            "Meet link también se envía por WA al número del caller — pero "
            "esto NO funciona para callers sin WA previo (restricción de "
            "Meta: no podemos iniciar conversaciones sin opt-in). El correo "
            "es el canal de entrega CONFIABLE — siempre pídelo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string", "description": "ISO datetime del inicio"},
                "end_iso": {"type": "string", "description": "ISO datetime del fin"},
                "attendee_email": {"type": "string", "description": "Email del prospecto, requerido"},
                "attendee_name": {"type": "string", "description": "Nombre del prospecto"},
                "clinic_name": {"type": "string", "description": "Nombre del consultorio o negocio"},
            },
            "required": ["start_iso", "end_iso", "attendee_email", "attendee_name"],
        },
    },
    {
        "type": "function",
        "name": "list_my_meetings",
        "description": (
            "Busca las próximas citas del usuario en el calendario de Gustavo. "
            "Solo devuelve citas donde el usuario es invitado (verifica por email). "
            "Úsala cuando el usuario quiera consultar, cancelar o reagendar sus citas. "
            "SIEMPRE pídele su correo electrónico antes de llamar esta función."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attendee_email": {
                    "type": "string",
                    "description": "Email del usuario (el invitado a la reunión)",
                },
                "days_ahead": {
                    "type": "integer",
                    "description": "Cuántos días hacia adelante buscar (default 90)",
                },
            },
            "required": ["attendee_email"],
        },
    },
    {
        "type": "function",
        "name": "cancel_meeting",
        "description": (
            "Cancela una cita existente. Solo se puede cancelar si el email "
            "coincide con el invitado original. ANTES de llamar esta función: "
            "(1) usa list_my_meetings para obtener event_id, (2) describe la cita "
            "al usuario, (3) pide confirmación explícita por voz. "
            "Google enviará automáticamente correo de cancelación."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID de la cita (de list_my_meetings)"},
                "attendee_email": {"type": "string", "description": "Email, debe coincidir con el invitado"},
            },
            "required": ["event_id", "attendee_email"],
        },
    },
    {
        "type": "function",
        "name": "reschedule_meeting",
        "description": (
            "Mueve una cita existente a un nuevo horario. Solo si el email coincide "
            "con el invitado original. FLUJO: (1) list_my_meetings para event_id, "
            "(2) get_available_slots para nuevos horarios, (3) usuario elige, "
            "(4) llama reschedule_meeting. Google envía correo de actualización."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID de la cita a reagendar"},
                "new_start_iso": {"type": "string", "description": "ISO del nuevo inicio (de get_available_slots)"},
                "new_end_iso": {"type": "string", "description": "ISO del nuevo fin"},
                "attendee_email": {"type": "string", "description": "Email, debe coincidir con el invitado"},
            },
            "required": ["event_id", "new_start_iso", "new_end_iso", "attendee_email"],
        },
    },
]


@dataclass(frozen=True)
class ToolContext:
    pool: asyncpg.Pool
    wa_id: str


async def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """
    Despacha una tool y devuelve el resultado serializado como string (lo que
    OpenAI Realtime espera en `function_call_output.output`).
    """
    try:
        if name == "get_available_slots":
            target_date = args.get("target_date") or None
            slots = await get_available_slots(
                ctx.pool,
                days_ahead=args.get("days_ahead", 14),
                target_date=target_date,
            )
            if not slots:
                msg = (
                    f"No hay horarios libres el {target_date}. Ofrece otra fecha o "
                    f"vuelve a llamar sin target_date para ver las opciones default."
                    if target_date
                    else "No hay horarios disponibles próximos"
                )
                return json.dumps({"slots": [], "message": msg})
            return json.dumps({"slots": slots}, default=str)

        if name == "book_meeting":
            result = await book_meeting(
                ctx.pool,
                start_iso=args["start_iso"],
                end_iso=args["end_iso"],
                attendee_email=args["attendee_email"],
                attendee_name=args["attendee_name"],
                clinic_name=args.get("clinic_name", ""),
                wa_id=ctx.wa_id,
            )
            return json.dumps(result, default=str)

        if name == "list_my_meetings":
            meetings = await list_user_meetings(
                ctx.pool,
                attendee_email=args["attendee_email"],
                days_ahead=args.get("days_ahead", 90),
            )
            if not meetings:
                return json.dumps({
                    "meetings": [],
                    "message": "No se encontraron citas con ese correo. ¿Es el correo correcto?",
                })
            return json.dumps({"meetings": meetings}, default=str)

        if name == "cancel_meeting":
            result = await cancel_meeting(
                ctx.pool,
                event_id=args["event_id"],
                attendee_email=args["attendee_email"],
                wa_id=ctx.wa_id,
            )
            return json.dumps(result, default=str)

        if name == "reschedule_meeting":
            result = await reschedule_meeting(
                ctx.pool,
                event_id=args["event_id"],
                new_start_iso=args["new_start_iso"],
                new_end_iso=args["new_end_iso"],
                attendee_email=args["attendee_email"],
                wa_id=ctx.wa_id,
            )
            return json.dumps(result, default=str)

        logger.warning("unknown tool: %s", name)
        return json.dumps({"error": f"Unknown tool: {name}"})

    except KeyError as e:
        logger.error("tool %s missing required arg: %s", name, e)
        return json.dumps({"error": f"Missing required argument: {e}"})
    except Exception:
        logger.exception("tool %s execution failed", name)
        return json.dumps({"error": "Tool execution failed unexpectedly"})
