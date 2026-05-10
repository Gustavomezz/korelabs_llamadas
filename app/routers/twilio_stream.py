"""
WebSocket /twilio/media-stream — STUB (Fase 2 implementa el bridge real).

Por ahora solo aceptamos el WS, parseamos los eventos `start`/`media`/`stop`
y registramos métricas básicas. No reproducimos audio. Esto deja Fase 1
verificable end-to-end (Twilio puede conectarse sin error y vemos los
eventos en logs) sin acoplar todavía al bridge a OpenAI Realtime.
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import logger

router = APIRouter(tags=["twilio"])


@router.websocket("/twilio/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    call_sid = None
    tenant_id = None
    media_frames = 0
    try:
        while True:
            message = await ws.receive_text()
            event = json.loads(message)
            kind = event.get("event")
            if kind == "start":
                start = event.get("start", {})
                call_sid = start.get("callSid")
                custom = start.get("customParameters", {}) or {}
                tenant_id = custom.get("tenant_id")
                logger.info(
                    "media-stream started CallSid=%s tenant=%s streamSid=%s",
                    call_sid, tenant_id, start.get("streamSid"),
                )
            elif kind == "media":
                media_frames += 1
                if media_frames % 200 == 0:
                    logger.debug("media frames received CallSid=%s n=%s", call_sid, media_frames)
            elif kind == "stop":
                logger.info(
                    "media-stream stopped CallSid=%s frames=%s", call_sid, media_frames,
                )
                break
    except WebSocketDisconnect:
        logger.info("media-stream disconnected CallSid=%s frames=%s", call_sid, media_frames)
    except Exception:
        logger.exception("media-stream error CallSid=%s", call_sid)
        try:
            await ws.close()
        except Exception:
            pass
