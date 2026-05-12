"""
Cliente HTTP para WhatsApp Business Cloud API.

Lift mínimo del repo korelabs-whatsapp-bot — solo `send_whatsapp_message`
(texto) y un extractor del message_id. Si las env vars no están seteadas,
las funciones hacen no-op y devuelven None (el booking de la cita NO se
bloquea por esto; el correo de Google Calendar sigue funcionando).

Usa las MISMAS credenciales que el bot de WhatsApp del tenant — un solo
número de Meta Business por tenant para ambos canales.
"""
import httpx

from app.config import logger, settings


def is_configured() -> bool:
    return bool(settings.whatsapp_token and settings.whatsapp_phone_number_id)


def _messages_url() -> str:
    return (
        f"https://graph.facebook.com/{settings.graph_api_version}/"
        f"{settings.whatsapp_phone_number_id}/messages"
    )


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }


def extract_wa_message_id(response: dict) -> str | None:
    try:
        return response.get("messages", [{}])[0].get("id")
    except (TypeError, IndexError, AttributeError):
        return None


async def send_whatsapp_message(to: str, text: str) -> dict | None:
    """Envía un mensaje de texto al wa_id. Devuelve la respuesta de Meta
    (incluye message_id) o None si falla / no está configurado."""
    if not is_configured():
        logger.info("whatsapp: no configurado (faltan env vars), saltando envío a %s", to)
        return None
    if not to:
        logger.warning("whatsapp: 'to' vacío, no se envía")
        return None

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(_messages_url(), json=payload, headers=_auth_headers())
            if r.status_code != 200:
                logger.error("whatsapp send error %d to=%s: %s", r.status_code, to, r.text)
                return None
            return r.json()
    except Exception:
        logger.exception("whatsapp send exception to=%s", to)
        return None
