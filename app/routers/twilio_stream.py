"""
WebSocket /twilio/media-stream — bridge Twilio <-> OpenAI Realtime.

Flujo:
  1. Twilio abre la WS al iniciar el <Connect><Stream/>.
  2. Aceptamos sin subprotocol (Twilio no negocia).
  3. Esperamos el evento `start` (trae streamSid, callSid y customParameters
     con tenant_id).
  4. Resolvemos pool del tenant, cargamos voice_prompt activo, buscamos
     call_id por call_sid.
  5. Abrimos sesión OpenAI Realtime con el prompt + greeting.
  6. Marcamos `calls.status='in-progress'` (answered_at se setea por el
     trigger del UPDATE).
  7. Lanzamos AudioBridge.run() — bloquea hasta que termine la llamada.
  8. Cleanup automático por context managers.
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from datetime import datetime, timezone

from app.ai.tools import REALTIME_TOOLS
from app.config import logger
from app.database import dashboard_pool, get_tenant_pool
from app.integrations.dashboard_db import fetch_tenant_by_voice_number
from app.models.bot_configs import get_active_voice_prompt
from app.models.calls import get_call_by_sid, update_call_status
from app.models.conversations import WhatsAppContext, get_recent_whatsapp_context
from app.realtime.audio_bridge import AudioBridge
from app.realtime.openai_session import open_session
from app.tenant_resolver import normalize_e164_to_wa_id

router = APIRouter(tags=["twilio"])

# Greeting hint: HARDCODEAMOS la apertura textual del primer turno.
# Probamos hints abstractos ("sigue tu system prompt", "haz tu pregunta
# principal") y el modelo improvisa cosas genéricas ("¿en qué te puedo
# ayudar?"). El response.create.instructions tiene prioridad sobre el
# system prompt para ESE turno, así que aquí dictamos el opener exacto.
# Si la pregunta principal cambia, hay que actualizar este string Y
# (opcionalmente) el system prompt en openai.com en sincronía.
GREETING_HINT = (
    "Di textualmente y nada más: "
    '"Hola, te habla Kora, asistente comercial de Korelabs. ¿Con quién '
    'tengo el gusto?". '
    "Eso es TODO tu primer turno. No preguntes 'en qué te puedo ayudar', "
    "no ofrezcas agendar todavía, no menciones automatización. "
    "Después de esta pregunta, espera la respuesta del usuario."
)


def _format_wa_recent_messages(messages: list[dict], max_chars: int = 250) -> str:
    """Formatea los últimos mensajes WA para inyectar al system prompt.
    Trunca contenido largo, etiqueta roles en español."""
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for m in messages:
        role = "Usuario" if m["role"] == "user" else "Kora"
        content = (m["content"] or "").strip().replace("\n", " ")
        if not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars] + "…"
        ts = m.get("created_at")
        when = ""
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = now - ts
            if delta.days >= 1:
                when = f" [hace {delta.days} día{'s' if delta.days > 1 else ''}]"
            elif delta.seconds >= 3600:
                when = f" [hace {delta.seconds // 3600}h]"
            else:
                when = " [hace minutos]"
        lines.append(f"  {role}{when}: {content}")
    return "\n".join(lines)


def _build_wa_context_block(ctx: WhatsAppContext) -> str:
    """Construye el bloque [CONTEXTO PREVIO POR WHATSAPP] que se appendea
    al system prompt. Le da al modelo el nombre, calificación previa, y
    los últimos mensajes para que retome la conversación con sentido."""
    name = ctx.get("name") or None
    clinic = ctx.get("clinic_name") or None
    qualified = ctx.get("qualified", False)
    total = ctx.get("total_messages", 0)
    recent = ctx.get("recent_messages", []) or []

    parts = ["[CONTEXTO PREVIO POR WHATSAPP — CRÍTICO]"]
    parts.append(
        f"Este caller ya conversó contigo por WhatsApp ({total} mensajes "
        "totales). NO lo trates como primera interacción."
    )

    facts: list[str] = []
    if name:
        facts.append(f"Nombre: {name}")
    if clinic:
        facts.append(f"Negocio/consultorio: {clinic}")
    if qualified:
        facts.append("Estado: ya calificado por el bot de WhatsApp")
    if facts:
        parts.append("Datos que YA tienes (NO los vuelvas a preguntar):")
        for f in facts:
            parts.append(f"  - {f}")

    if recent:
        parts.append("")
        parts.append("Últimos mensajes intercambiados por WhatsApp (orden cronológico):")
        parts.append(_format_wa_recent_messages(recent))

    parts.append("")
    parts.append(
        "DIRECCIÓN DE LA LLAMADA: es INBOUND — el usuario te llamó a ti. "
        "NO digas 'te marco', 'te llamo', 'te contacto' o similares — "
        "implicarían que tú iniciaste, lo cual es falso.\n\n"
        "REGLAS DE SEGUIMIENTO (LEER CON CUIDADO):\n"
        "1. PRIMER TURNO: solo saluda usando el primer nombre del usuario "
        "y haz UNA pregunta abierta corta tipo '¿en qué te puedo ayudar?'. "
        "DESPUÉS te callas Y ESPERAS QUE EL USUARIO HABLE. NO sigas al "
        "Paso 2 del system prompt. NO ofrezcas agendar todavía. NO "
        "menciones la conversación previa. NO encadenas dos preguntas.\n"
        "2. NO repitas preguntas cuya respuesta ya está arriba (nombre, "
        "negocio, correo, etc.) — esos datos YA los tienes y NO los pides "
        "de nuevo. Pero TENER los datos NO significa avanzar al siguiente "
        "paso del flujo sin escuchar al usuario.\n"
        "3. SOLO DESPUÉS de que el usuario haya hablado, evalúas el "
        "[CONTEXTO PREVIO POR WHATSAPP] y decides cómo seguir: si estaba "
        "a punto de agendar, ofrécelo; si tenía una cita ya agendada, "
        "podría querer confirmar/reagendar; etc."
    )
    return "\n".join(parts)


def _build_returning_user_greeting_hint(ctx: WhatsAppContext) -> str:
    """Greeting alternativo cuando el caller ya tiene historial WA.

    INSTRUCCIÓN CRÍTICA: el modelo SOLO debe decir esta frase y callar.
    Sin esto el modelo encadena automáticamente el Paso 2 del system
    prompt (ofrecer agendar) porque ya tiene el nombre del wa_context."""
    name = ctx.get("name") or None
    first_name = name.split()[0] if name else None
    nombre_part = f" {first_name}" if first_name else ""
    return (
        f'Tu PRIMER turno completo es EXACTAMENTE esta frase, nada más: '
        f'"¡Hola{nombre_part}! Habla Kora de Korelabs. ¿En qué te puedo '
        f'ayudar?". '
        "DESPUÉS de decir esa frase, te callas y ESPERAS que el usuario "
        "hable. NO encadenes una segunda pregunta. NO ofrezcas agendar. "
        "NO menciones WhatsApp. NO digas 'mucho gusto'. NO pases al Paso 2 "
        "del flujo. AUNQUE tengas todos los datos del usuario en el "
        "contexto previo, NO avanzas — esperas que él diga por qué llamó. "
        "Solo después de que el usuario hable, decides cómo continuar."
    )


@router.websocket("/twilio/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    call_sid: str | None = None
    tenant_id: str | None = None
    stream_sid: str | None = None
    caller_e164: str | None = None

    try:
        # Twilio manda: connected -> start -> media* -> stop
        # Esperamos el `start` antes de abrir nada hacia OpenAI.
        while True:
            event = json.loads(await ws.receive_text())
            kind = event.get("event")
            if kind == "start":
                start = event.get("start", {})
                call_sid = start.get("callSid")
                stream_sid = start.get("streamSid")
                params = start.get("customParameters", {}) or {}
                tenant_id = params.get("tenant_id")
                logger.info(
                    "media-stream start CallSid=%s tenant=%s streamSid=%s",
                    call_sid, tenant_id, stream_sid,
                )
                break
            elif kind == "stop":
                logger.warning("media-stream stop before start, aborting")
                return
    except WebSocketDisconnect:
        logger.info("media-stream disconnected before start")
        return
    except Exception:
        logger.exception("media-stream failed waiting start")
        return

    if not (call_sid and stream_sid and tenant_id):
        logger.error("missing fields after start CallSid=%s streamSid=%s tenant=%s",
                     call_sid, stream_sid, tenant_id)
        return

    # Resolver pool del tenant. tenant_id viene del custom param que pusimos
    # en el TwiML (el id numérico del registro `tenants`).
    try:
        # Necesitamos la database_url del tenant para abrir/recuperar pool.
        # Como ya está cacheado de Fase 1, la lookup es ligera; pero por si
        # la cache se vació (restart), resolvemos de nuevo por ID.
        async with dashboard_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT slug, voice_phone_number_e164 FROM tenants WHERE id = $1::integer",
                int(tenant_id),
            )
        if row is None:
            logger.error("tenant id=%s no longer exists", tenant_id)
            return

        tenant = await fetch_tenant_by_voice_number(dashboard_pool(), row["voice_phone_number_e164"])
        if tenant is None:
            logger.error("tenant lookup by voice number failed slug=%s", row["slug"])
            return
        pool = await get_tenant_pool(str(tenant.id), tenant.database_url)

        voice_prompt = await get_active_voice_prompt(pool)
        call_row = await get_call_by_sid(pool, call_sid)
        if call_row is None:
            logger.error("calls row not found for CallSid=%s", call_sid)
            return
        call_id = call_row["id"]
        wa_id = call_row["wa_id"] or normalize_e164_to_wa_id(call_row["caller_number"])

        # Si el caller tiene historial WhatsApp, inyectamos contexto +
        # cambiamos el opener. Lo de WA y el de voz comparten BD del tenant,
        # así que esto es una lookup ligera (~5-10ms). Si falla, seguimos
        # con flujo normal (no bloqueamos la llamada).
        wa_context: WhatsAppContext | None = None
        try:
            wa_context = await get_recent_whatsapp_context(pool, wa_id, limit=15)
        except Exception:
            logger.exception("WA context lookup failed wa_id=%s (continuing without)", wa_id)

        if wa_context:
            ctx_block = _build_wa_context_block(wa_context)
            voice_prompt = f"{voice_prompt}\n\n{ctx_block}"
            greeting_hint = _build_returning_user_greeting_hint(wa_context)
            logger.info(
                "wa context applied call_id=%s wa_id=%s name=%s msgs=%d qualified=%s",
                call_id, wa_id,
                wa_context.get("name") or "-",
                wa_context.get("total_messages", 0),
                wa_context.get("qualified", False),
            )
        else:
            greeting_hint = GREETING_HINT
            logger.info("no WA history for wa_id=%s — cold call greeting", wa_id)

        await update_call_status(pool, call_sid=call_sid, status="in-progress")
    except Exception:
        logger.exception("setup failed CallSid=%s", call_sid)
        return

    # Abrir sesión OpenAI Realtime y arrancar el bridge.
    try:
        async with open_session(
            instructions=voice_prompt,
            greeting_hint=greeting_hint,
            tools=REALTIME_TOOLS,
        ) as openai:
            bridge = AudioBridge(
                twilio_ws=ws, openai=openai, pool=pool,
                stream_sid=stream_sid, call_id=call_id, wa_id=wa_id,
            )
            await bridge.run()
    except Exception:
        logger.exception("bridge failed CallSid=%s", call_sid)
    finally:
        # Si Twilio aún no cerró por su lado, cerramos.
        try:
            await ws.close()
        except Exception:
            pass
