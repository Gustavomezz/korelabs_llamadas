"""Verifica que validate_signature acepta firmas válidas y rechaza alteraciones."""
from twilio.request_validator import RequestValidator

import app.integrations.twilio_client as tw_client
from app.config import settings


def _sign(token: str, url: str, params: dict) -> str:
    return RequestValidator(token).compute_signature(url, params)


def test_signature_valid(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test_token_xyz")
    url = "https://example.com/twilio/voice/incoming"
    params = {"CallSid": "CA123", "From": "+523321015972", "To": "+523321015972"}
    signature = _sign("test_token_xyz", url, params)
    assert tw_client.validate_signature(url, params, signature) is True


def test_signature_tampered_param(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "test_token_xyz")
    url = "https://example.com/twilio/voice/incoming"
    params = {"CallSid": "CA123", "From": "+523321015972", "To": "+523321015972"}
    signature = _sign("test_token_xyz", url, params)
    tampered = {**params, "From": "+19999999999"}
    assert tw_client.validate_signature(url, tampered, signature) is False


def test_signature_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "wrong_token")
    url = "https://example.com/twilio/voice/incoming"
    params = {"CallSid": "CA123", "From": "+523321015972", "To": "+523321015972"}
    signature = _sign("real_token", url, params)
    assert tw_client.validate_signature(url, params, signature) is False
