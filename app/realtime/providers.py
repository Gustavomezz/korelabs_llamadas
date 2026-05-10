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
    vad_threshold: float = 0.5,
    vad_silence_ms: int = 300,
    vad_prefix_ms: int = 200,
    max_output_tokens: int | None = None,
) -> dict:
    """
    Envelope para Grok Voice Think Fast 1.0.

    Grok forkea el protocolo de OpenAI Realtime así que casi todos los
    parámetros funcionan igual:
    - `turn_detection` con threshold/silence_duration_ms/prefix_padding_ms
      (mismos rangos que OpenAI: el default conservador del servicio causaba
      delay grande; con los mismos params agresivos que usamos en OpenAI
      mini, Grok debería bajar similar).
    - `max_response_output_tokens` no está documentado oficialmente pero como
      Grok forkea OpenAI lo enviamos optimistamente — si no lo soporta
      simplemente lo ignora (testeamos contra el API).

    NO soportado en Grok (vs OpenAI):
    - `reasoning.effort` (Grok dice que razona en background sin trade-off)
    - `prompt_id` server-stored
    - `transcription` para capturar lo que dice el user (puede no haber
      transcripts del usuario en BD — verificar tras primera llamada).
    - `output_modalities` y `session.type` (envelope sin esos campos).
    """
    session: dict[str, Any] = {
        "voice": voice,
        "instructions": instructions,
        "turn_detection": {
            "type": "server_vad",
            "threshold": vad_threshold,
            "prefix_padding_ms": vad_prefix_ms,
            "silence_duration_ms": vad_silence_ms,
            "create_response": True,
            "interrupt_response": True,
        },
        "audio": {
            "input": {"format": {"type": "audio/pcmu", "rate": 8000}},
            "output": {"format": {"type": "audio/pcmu", "rate": 8000}},
        },
    }
    if tools:
        session["tools"] = list(tools)
    if max_output_tokens:
        session["max_response_output_tokens"] = max_output_tokens
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
