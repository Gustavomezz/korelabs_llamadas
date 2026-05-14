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

from app.config import logger, settings


@dataclass(frozen=True)
class ProviderSpec:
    """Identidad y endpoint del proveedor en uso."""
    name: str  # 'openai' | 'grok'
    ws_url: str
    api_key: str
    extra_headers: dict[str, str] = field(default_factory=dict)


def get_provider() -> ProviderSpec:
    """Devuelve la spec del provider activo según settings.voice_provider.

    Normaliza el valor (strip + lower) para tolerar 'Grok', ' grok ', 'GROK', etc.
    Loggea explícitamente cuál se resolvió para hacer debuggable cualquier
    mismatch entre el env var de Railway y lo que efectivamente usa el bot.
    """
    raw = (settings.voice_provider or "").strip().lower()
    logger.info("provider resolver: raw='%s' normalized='%s'", settings.voice_provider, raw)
    if raw == "grok":
        if not settings.xai_api_key:
            raise RuntimeError("XAI_API_KEY no configurada (voice_provider=grok)")
        return ProviderSpec(
            name="grok",
            ws_url=f"wss://api.x.ai/v1/realtime?model={settings.xai_realtime_model}",
            api_key=settings.xai_api_key,
            extra_headers={},
        )
    if raw and raw != "openai":
        logger.warning(
            "voice_provider='%s' no reconocido — cayendo a openai (valores válidos: grok | openai)",
            settings.voice_provider,
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
    vad_threshold: float = 0.5,
    vad_silence_ms: int = 300,
    vad_prefix_ms: int = 200,
    max_output_tokens: int | None = None,
) -> dict:
    """
    Envelope para Grok Voice Think Fast 1.0.

    Schema exacto según docs oficiales xAI (verificado en
    https://docs.x.ai/developers/model-capabilities/audio/voice-agent):

      session: {
        instructions: string,
        voice: eve|ara|rex|sal|leo,
        tools: [...],
        turn_detection: {
          type: "server_vad"|null,
          threshold: 0.1–0.9 (default 0.85),
          silence_duration_ms: 0–10000,
          prefix_padding_ms: 0–10000 (default 333)
        },
        audio: { input/output: { format: { type, rate } } }
      }

    NO soportado en Grok (lo OMITIMOS — antes lo mandábamos y Grok
    silenciosamente rechazaba el session.update completo, cayendo a sus
    defaults conservadores y aumentando la latencia):
    - `create_response`, `interrupt_response` (OpenAI-isms en turn_detection)
    - `max_response_output_tokens`
    - `reasoning.effort`
    - `prompt_id` server-stored
    - `transcription`
    - `output_modalities` y `session.type`
    """
    session: dict[str, Any] = {
        "voice": voice,
        "instructions": instructions,
        "turn_detection": {
            "type": "server_vad",
            "threshold": vad_threshold,
            "prefix_padding_ms": vad_prefix_ms,
            "silence_duration_ms": vad_silence_ms,
        },
        "audio": {
            "input": {"format": {"type": "audio/pcmu", "rate": 8000}},
            "output": {"format": {"type": "audio/pcmu", "rate": 8000}},
        },
    }
    if tools:
        session["tools"] = list(tools)
    # max_output_tokens ignorado en Grok — no está en su schema documentado.
    # Para limitar largo, controlar desde el system prompt directamente.
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
