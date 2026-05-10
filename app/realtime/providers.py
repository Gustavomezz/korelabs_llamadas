"""
Adaptadores por proveedor de voz realtime (OpenAI vs Grok/xAI).

Ambos exponen una WebSocket con session.update + audio bidireccional, pero
con diferencias suficientes en el envelope para no poder reusar el mismo
builder. Esta capa traduce nuestra config interna al payload exacto de cada
provider, y normaliza nombres de eventos para que el bridge los maneje
transparentemente.
"""
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.config import settings


@dataclass(frozen=True)
class ProviderSpec:
    """Identidad y endpoint del proveedor en uso."""
    name: str  # 'openai' | 'grok'
    ws_url: str
    api_key: str
    extra_headers: dict[str, str] = field(default_factory=dict)


def get_provider() -> ProviderSpec:
    """Devuelve la spec del provider activo según settings.voice_provider."""
    if settings.voice_provider == "grok":
        if not settings.xai_api_key:
            raise RuntimeError("XAI_API_KEY no configurada (voice_provider=grok)")
        return ProviderSpec(
            name="grok",
            ws_url=f"wss://api.x.ai/v1/realtime?model={settings.xai_realtime_model}",
            api_key=settings.xai_api_key,
            extra_headers={},
        )
    # default: openai
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    headers: dict[str, str] = {}
    # Beta header solo para modelos v1 legacy de OpenAI; v2 lo prohíbe.
    from app.realtime.events import is_v2_model  # local import evita ciclo
    if not is_v2_model(settings.openai_realtime_model):
        headers["OpenAI-Beta"] = "realtime=v1"
    return ProviderSpec(
        name="openai",
        ws_url=f"wss://api.openai.com/v1/realtime?model={settings.openai_realtime_model}",
        api_key=settings.openai_api_key,
        extra_headers=headers,
    )


def auth_headers(spec: ProviderSpec) -> dict[str, str]:
    """Headers HTTP para abrir la WebSocket."""
    return {"Authorization": f"Bearer {spec.api_key}", **spec.extra_headers}


def build_session_update_grok(
    *,
    instructions: str,
    voice: str,
    tools: Iterable[dict] | None,
) -> dict:
    """
    Envelope para Grok Voice Think Fast 1.0:
    - Sin session.type ni output_modalities.
    - voice y turn_detection top-level.
    - audio.input.format / audio.output.format con type=audio/pcmu para Twilio.
    - Sin reasoning (Grok hace background reasoning sin trade-off).
    - Sin transcription documentada (puede no haber transcripts del user).
    """
    session: dict[str, Any] = {
        "voice": voice,
        "instructions": instructions,
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {"format": {"type": "audio/pcmu", "rate": 8000}},
            "output": {"format": {"type": "audio/pcmu", "rate": 8000}},
        },
    }
    if tools:
        session["tools"] = list(tools)
    return {"type": "session.update", "session": session}


def build_greeting_events_grok(greeting_hint: str | None) -> list[dict]:
    """
    Grok no acepta `response.create` con instructions inline para forzar
    saludo. En su lugar manda un mensaje de usuario implícito + response.create.
    """
    hint = greeting_hint or "Saluda con calidez en español."
    return [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": hint}],
            },
        },
        {"type": "response.create"},
    ]
