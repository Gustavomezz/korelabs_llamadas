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

from app.ai.tools import REALTIME_TOOLS
from app.config import logger
from app.database import dashboard_pool, get_tenant_pool
from app.integrations.dashboard_db import fetch_tenant_by_voice_number
from app.models.bot_configs import get_active_voice_prompt
from app.models.calls import get_call_by_sid, update_call_status
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
    "Saluda muy breve y di textualmente: "
    '"Hola, soy Kora, de Korelabs. ¿Quieres automatizar tu servicio al '
    'cliente, o procesos internos de tu negocio?". '
    "Eso es TODO tu primer turno. No preguntes 'en qué te puedo ayudar', "
    "no pidas el nombre todavía, no menciones agendar/revisar/mover citas. "
    "Después de esta pregunta, espera la respuesta del usuario."
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

        await update_call_status(pool, call_sid=call_sid, status="in-progress")
    except Exception:
        logger.exception("setup failed CallSid=%s", call_sid)
        return

    # Abrir sesión OpenAI Realtime y arrancar el bridge.
    try:
        async with open_session(
            instructions=voice_prompt,
            greeting_hint=GREETING_HINT,
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
