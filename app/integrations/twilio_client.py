"""Validación de webhook signature de Twilio."""
from typing import Mapping

from twilio.request_validator import RequestValidator

from app.config import settings


def _validator() -> RequestValidator:
    if not settings.twilio_auth_token:
        raise RuntimeError("TWILIO_AUTH_TOKEN not configured")
    return RequestValidator(settings.twilio_auth_token)


def validate_signature(url: str, params: Mapping[str, str], signature: str) -> bool:
    """
    Valida la firma X-Twilio-Signature.

    `url` debe ser la URL absoluta exacta que Twilio invocó (con esquema y query).
    `params` son los form-encoded params del POST (o {} si fue GET).
    """
    return _validator().validate(url, dict(params), signature)
