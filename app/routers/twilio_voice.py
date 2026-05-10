"""
Webhooks de Twilio Voice.

Flujo de una llamada entrante:
    Twilio POST /twilio/voice/incoming  (form-encoded, X-Twilio-Signature)
        -> validamos signature
        -> resolvemos tenant por número marcado (To)
        -> upsertamos contacto + creamos fila `calls`
        -> respondemos TwiML con <Connect><Stream url="wss://.../twilio/media-stream"/>
        -> Twilio abre WS al stream (lo maneja Fase 2)

El status callback (lifecycle de la llamada) vive en twilio_status.py.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, HTTPException, Request, Response

from app.config import logger, settings
from app.integrations.twilio_client import validate_signature
from app.models.calls import create_call, upsert_contact
from app.tenant_resolver import normalize_e164_to_wa_id, resolve_by_voice_number

router = APIRouter(prefix="/twilio/voice", tags=["twilio"])


def _absolute_url(request: Request) -> str:
    """
    Reconstruye la URL absoluta exacta como Twilio la firmó.

    Cuando estamos detrás del proxy de Railway, request.url ya incluye scheme
    correcto si Railway propaga X-Forwarded-Proto, pero si no, forzamos
    settings.public_base_url para evitar mismatch http/https en signature.
    """
    if settings.public_base_url:
        path_qs = request.url.path
        if request.url.query:
            path_qs += f"?{request.url.query}"
        return settings.public_base_url.rstrip("/") + path_qs
    return str(request.url)


def _twiml_stream(stream_url: str, call_sid: str, tenant_id: str) -> str:
    """TwiML que abre un Media Stream bidireccional al WS del bridge."""
    params = (
        f'<Parameter name="call_sid" value="{call_sid}"/>'
        f'<Parameter name="tenant_id" value="{tenant_id}"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{stream_url}">{params}</Stream></Connect>'
        "</Response>"
    )


@router.post("/incoming")
async def incoming(
    request: Request,
    x_twilio_signature: str = Header(default=""),
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
):
    body = await request.form()
    url = _absolute_url(request)
    if not validate_signature(url, dict(body), x_twilio_signature):
        logger.warning("twilio signature invalid for CallSid=%s", CallSid)
        raise HTTPException(status_code=403, detail="invalid signature")

    resolved = await resolve_by_voice_number(To)
    if resolved is None or not resolved.record.voice_enabled:
        logger.warning("no voice tenant for To=%s CallSid=%s", To, CallSid)
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say language=\"es-MX\">"
                "Lo sentimos, este número no está disponible en este momento."
                "</Say><Hangup/></Response>"
            ),
            media_type="application/xml",
        )

    wa_id = normalize_e164_to_wa_id(From)
    await upsert_contact(resolved.pool, wa_id)
    await create_call(
        resolved.pool,
        call_sid=CallSid,
        caller_number=From,
        to_number=To,
        wa_id=wa_id,
        metadata={"tenant_slug": resolved.record.slug},
    )

    if not settings.public_base_url:
        logger.error("PUBLIC_BASE_URL not set; cannot build stream WSS")
        raise HTTPException(status_code=500, detail="server misconfigured")
    wss_base = settings.public_base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{wss_base}/twilio/media-stream"

    twiml = _twiml_stream(stream_url, CallSid, str(resolved.record.id))
    logger.info(
        "incoming call routed CallSid=%s tenant=%s wa_id=%s",
        CallSid, resolved.record.slug, wa_id,
    )
    return Response(content=twiml, media_type="application/xml")
