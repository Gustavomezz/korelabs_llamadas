"""
Status callbacks de Twilio Voice.

Twilio dispara webhooks en cada cambio de estado de la llamada:
queued, ringing, in-progress, completed, busy, failed, no-answer, canceled.

Lo usamos para mantener `calls.status`, `duration_seconds`, etc. al día,
sin depender exclusivamente de lo que el bridge WS detecte.
"""
from typing import Optional

from fastapi import APIRouter, Form, Header, HTTPException, Request

from app.config import logger
from app.integrations.twilio_client import validate_signature
from app.models.calls import update_call_status
from app.tenant_resolver import resolve_by_voice_number
from app.routers.twilio_voice import _absolute_url

router = APIRouter(prefix="/twilio/voice", tags=["twilio"])


@router.post("/status")
async def status(
    request: Request,
    x_twilio_signature: str = Header(default=""),
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
    To: str = Form(...),
    CallDuration: Optional[str] = Form(default=None),
    RecordingUrl: Optional[str] = Form(default=None),
):
    body = await request.form()
    if not validate_signature(_absolute_url(request), dict(body), x_twilio_signature):
        logger.warning("twilio status signature invalid for CallSid=%s", CallSid)
        raise HTTPException(status_code=403, detail="invalid signature")

    resolved = await resolve_by_voice_number(To)
    if resolved is None:
        logger.warning("status callback for unknown To=%s CallSid=%s", To, CallSid)
        return {"status": "ignored"}

    duration: Optional[int] = None
    if CallDuration and CallDuration.isdigit():
        duration = int(CallDuration)

    await update_call_status(
        resolved.pool,
        call_sid=CallSid,
        status=CallStatus,
        duration_seconds=duration,
        recording_url=RecordingUrl,
    )
    logger.info("status update CallSid=%s status=%s", CallSid, CallStatus)
    return {"status": "ok"}
