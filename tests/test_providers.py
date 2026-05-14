"""
Tests del builder de session.update por provider y greeting.

Mockea settings via monkeypatch para no tocar env vars reales.
"""
from app.config import settings
from app.realtime.providers import (
    auth_headers,
    build_greeting_events_grok,
    build_session_update_grok,
    get_provider,
)
from app.realtime.events import build_greeting_events_openai


def test_grok_session_update_envelope_with_aggressive_vad():
    evt = build_session_update_grok(
        instructions="Hola Kora", voice="ara", tools=None,
    )
    s = evt["session"]
    assert evt["type"] == "session.update"
    assert s["instructions"] == "Hola Kora"
    assert s["voice"] == "ara"  # voice top-level (no como OpenAI v2 que va anidado)
    # Default VAD agresivo: 300ms silence, threshold 0.5 (mismo que OpenAI)
    td = s["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.5
    assert td["silence_duration_ms"] == 300
    assert td["prefix_padding_ms"] == 200
    assert "interrupt_response" not in td
    # Audio anidado, mismo formato que OpenAI para Twilio
    assert s["audio"]["input"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcmu", "rate": 8000}
    # Grok NO tiene estos campos
    assert "type" not in s  # No session.type
    assert "output_modalities" not in s
    assert "reasoning" not in s
    assert "tool_choice" not in s  # opcional, no lo mandamos por default


def test_grok_session_update_omits_unsupported_max_tokens():
    """Grok no documenta max tokens en session.update; no mandamos campos extra."""
    evt = build_session_update_grok(
        instructions="x", voice="ara", tools=None, max_output_tokens=300,
    )
    assert "max_response_output_tokens" not in evt["session"]
    assert "max_output_tokens" not in evt["session"]


def test_grok_session_update_can_tune_vad():
    evt = build_session_update_grok(
        instructions="x", voice="eve", tools=None,
        vad_threshold=0.7, vad_silence_ms=200, vad_prefix_ms=150,
    )
    td = evt["session"]["turn_detection"]
    assert td["threshold"] == 0.7
    assert td["silence_duration_ms"] == 200
    assert td["prefix_padding_ms"] == 150


def test_grok_session_update_with_tools():
    tools = [{"type": "function", "name": "ping", "description": "x", "parameters": {}}]
    evt = build_session_update_grok(instructions="x", voice="eve", tools=tools)
    assert evt["session"]["tools"] == tools


def test_grok_greeting_uses_user_message_pattern():
    """Grok no acepta response.create con instructions; usa user msg + create."""
    events = build_greeting_events_grok("Saluda al cliente")
    assert len(events) == 2
    assert events[0]["type"] == "conversation.item.create"
    assert events[0]["item"]["role"] == "user"
    assert events[0]["item"]["content"][0]["text"] == "Saluda al cliente"
    assert events[1] == {"type": "response.create"}


def test_grok_greeting_default_when_no_hint():
    events = build_greeting_events_grok(None)
    assert "Saluda" in events[0]["item"]["content"][0]["text"]


def test_openai_greeting_uses_response_create():
    events = build_greeting_events_openai("Saluda con calidez")
    assert len(events) == 1
    assert events[0]["type"] == "response.create"
    assert events[0]["response"]["instructions"] == "Saluda con calidez"


def test_get_provider_openai_default(monkeypatch):
    monkeypatch.setattr(settings, "voice_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_realtime_model", "gpt-realtime-2")
    spec = get_provider()
    assert spec.name == "openai"
    assert spec.ws_url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"
    assert "OpenAI-Beta" not in spec.extra_headers  # v2 no manda beta


def test_get_provider_openai_v1_sends_beta_header(monkeypatch):
    monkeypatch.setattr(settings, "voice_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_realtime_model", "gpt-realtime")
    spec = get_provider()
    assert spec.extra_headers["OpenAI-Beta"] == "realtime=v1"


def test_get_provider_grok(monkeypatch):
    monkeypatch.setattr(settings, "voice_provider", "grok")
    monkeypatch.setattr(settings, "xai_api_key", "xai-test")
    monkeypatch.setattr(settings, "xai_realtime_model", "grok-voice-think-fast-1.0")
    spec = get_provider()
    assert spec.name == "grok"
    assert spec.ws_url == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-1.0"
    assert spec.api_key == "xai-test"


def test_auth_headers_includes_bearer():
    from app.realtime.providers import ProviderSpec
    spec = ProviderSpec(name="openai", ws_url="wss://x", api_key="sk-abc",
                       extra_headers={"OpenAI-Beta": "realtime=v1"})
    h = auth_headers(spec)
    assert h["Authorization"] == "Bearer sk-abc"
    assert h["OpenAI-Beta"] == "realtime=v1"
