"""
Tests del endpoint /twilio/voice/incoming.

Mockeamos tenant_resolver, modelos de calls, y validate_signature para
ejercitar el flujo sin Postgres ni Twilio reales. Lo que valida:

- Si la signature es inválida -> 403.
- Si el tenant no existe / voice_enabled=false -> TwiML <Say>+<Hangup>.
- Si el tenant existe -> TwiML <Connect><Stream> con custom params correctos
  y se llamó a upsert_contact + create_call.
"""
import pytest
from fastapi.testclient import TestClient

import app.routers.twilio_voice as voice_module
from app.config import settings
from app.integrations.dashboard_db import TenantRecord
from app.tenant_resolver import ResolvedTenant
from main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://llamadas.test")
    monkeypatch.setattr(settings, "twilio_auth_token", "any")
    return TestClient(app)


def _form(extra: dict | None = None) -> dict:
    base = {
        "CallSid": "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "From": "+523321015972",
        "To": "+523321015972",
    }
    if extra:
        base.update(extra)
    return base


def test_incoming_invalid_signature_returns_403(client, monkeypatch):
    monkeypatch.setattr(voice_module, "validate_signature", lambda *a, **kw: False)
    resp = client.post("/twilio/voice/incoming", data=_form(), headers={"X-Twilio-Signature": "bogus"})
    assert resp.status_code == 403


def test_incoming_unknown_tenant_returns_say_hangup(client, monkeypatch):
    monkeypatch.setattr(voice_module, "validate_signature", lambda *a, **kw: True)

    async def fake_resolve(_):
        return None
    monkeypatch.setattr(voice_module, "resolve_by_voice_number", fake_resolve)

    resp = client.post("/twilio/voice/incoming", data=_form(), headers={"X-Twilio-Signature": "ok"})
    assert resp.status_code == 200
    body = resp.text
    assert "<Say" in body and "<Hangup/>" in body
    assert "<Connect>" not in body


def test_incoming_disabled_tenant_returns_say_hangup(client, monkeypatch):
    monkeypatch.setattr(voice_module, "validate_signature", lambda *a, **kw: True)

    record = TenantRecord(
        id=42, slug="korelabs", display_name="Korelabs",
        database_url="postgresql://x", voice_phone_number_e164="+523321015972",
        voice_enabled=False,
    )

    class _FakePool:
        pass

    async def fake_resolve(_):
        return ResolvedTenant(record=record, pool=_FakePool())
    monkeypatch.setattr(voice_module, "resolve_by_voice_number", fake_resolve)

    resp = client.post("/twilio/voice/incoming", data=_form(), headers={"X-Twilio-Signature": "ok"})
    assert resp.status_code == 200
    assert "<Hangup/>" in resp.text and "<Connect>" not in resp.text


def test_incoming_enabled_tenant_returns_connect_stream(client, monkeypatch):
    monkeypatch.setattr(voice_module, "validate_signature", lambda *a, **kw: True)

    record = TenantRecord(
        id=42, slug="korelabs", display_name="Korelabs",
        database_url="postgresql://x", voice_phone_number_e164="+523321015972",
        voice_enabled=True,
    )

    class _FakePool:
        pass

    resolved = ResolvedTenant(record=record, pool=_FakePool())

    async def fake_resolve(_):
        return resolved
    monkeypatch.setattr(voice_module, "resolve_by_voice_number", fake_resolve)

    upsert_calls = []
    create_calls = []

    async def fake_upsert(pool, wa_id):
        upsert_calls.append(wa_id)

    async def fake_create(pool, **kwargs):
        create_calls.append(kwargs)
        return 1

    monkeypatch.setattr(voice_module, "upsert_contact", fake_upsert)
    monkeypatch.setattr(voice_module, "create_call", fake_create)

    resp = client.post("/twilio/voice/incoming", data=_form(), headers={"X-Twilio-Signature": "ok"})
    assert resp.status_code == 200
    body = resp.text

    assert '<Connect><Stream url="wss://llamadas.test/twilio/media-stream">' in body
    assert '<Parameter name="call_sid" value="CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"/>' in body
    assert '<Parameter name="tenant_id" value="42"/>' in body

    assert upsert_calls == ["523321015972"]
    assert len(create_calls) == 1
    assert create_calls[0]["call_sid"] == "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert create_calls[0]["wa_id"] == "523321015972"
    assert create_calls[0]["caller_number"] == "+523321015972"
