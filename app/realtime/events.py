"""
Helpers puros para construir/parsear los eventos de OpenAI Realtime y Twilio
Media Streams. Sin I/O — fáciles de testear.

OpenAI tiene dos versiones de envelope incompatibles:
- v1: usado por `gpt-realtime`, `gpt-realtime-1.5`, `gpt-4o-realtime-preview-*`.
  Header `OpenAI-Beta: realtime=v1`. Campos planos. Eventos
  `response.audio.delta`, `response.audio_transcript.done`.
- v2: usado por `gpt-realtime-2` y posteriores (lanzado 2026-05-07).
  SIN header beta. `session.type: "realtime"` requerido. Campos de audio
  anidados bajo `audio.input` / `audio.output`. Eventos
  `response.output_audio.delta`, `response.output_audio_transcript.done`.

Detectamos automáticamente la versión por el nombre del modelo y construimos
el envelope correcto. Los handlers del bridge aceptan ambos nombres de
evento para que el cambio de modelo no rompa el código.

Referencia: ver memoria openai_realtime_v2_breaking_changes para la tabla
completa de cambios entre versiones.
"""
import json
from typing import Any, Iterable


def is_v2_model(model: str) -> bool:
    """gpt-realtime-2, gpt-realtime-2.5, etc. usan el envelope nuevo.
    gpt-realtime, gpt-realtime-1.5, gpt-4o-realtime-* usan el viejo."""
    name = (model or "").lower()
    if name.startswith("gpt-realtime-2") or name.startswith("gpt-realtime-3"):
        return True
    return False


def session_update(
    *,
    instructions: str,
    model: str,
    voice: str = "cedar",
    temperature: float = 0.8,
    tools: Iterable[dict] | None = None,
    vad_threshold: float = 0.7,
    vad_silence_ms: int = 700,
    vad_prefix_ms: int = 300,
    reasoning_effort: str = "low",
) -> dict:
    """
    Construye `session.update` para Realtime. Audio g711 µ-law end-to-end
    (lo que manda Twilio), VAD server-side, transcripción del usuario con
    whisper-1.
    """
    if is_v2_model(model):
        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": vad_threshold,
                        "prefix_padding_ms": vad_prefix_ms,
                        "silence_duration_ms": vad_silence_ms,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice,
                    "speed": 1.0,
                },
            },
            "tool_choice": "auto",
        }
        # reasoning.effort es opcional; "low" minimiza latencia y costo,
        # "medium"/"high" para consultas más complejas.
        if reasoning_effort:
            session["reasoning"] = {"effort": reasoning_effort}
        if tools:
            session["tools"] = list(tools)
        return {"type": "session.update", "session": session}

    # Envelope v1 (legacy): para gpt-realtime, gpt-realtime-1.5, etc.
    session = {
        "modalities": ["audio", "text"],
        "instructions": instructions,
        "voice": voice,
        "input_audio_format": "g711_ulaw",
        "output_audio_format": "g711_ulaw",
        "input_audio_transcription": {"model": "whisper-1"},
        "turn_detection": {
            "type": "server_vad",
            "threshold": vad_threshold,
            "prefix_padding_ms": vad_prefix_ms,
            "silence_duration_ms": vad_silence_ms,
            "create_response": True,
        },
        "temperature": temperature,
        "tool_choice": "auto",
    }
    if tools:
        session["tools"] = list(tools)
    return {"type": "session.update", "session": session}


def initial_response_create(greeting_hint: str | None = None) -> dict:
    """
    Fuerza al modelo a hablar primero (saludo). Sin esto, OpenAI espera input
    de audio del usuario y la llamada queda en silencio.
    """
    payload: dict[str, Any] = {"type": "response.create"}
    if greeting_hint:
        payload["response"] = {"instructions": greeting_hint}
    return payload


def append_audio(payload_b64: str) -> dict:
    """`media.payload` de Twilio ya viene en base64 µ-law: pasa directo."""
    return {"type": "input_audio_buffer.append", "audio": payload_b64}


def function_call_output(call_id: str, output: Any) -> dict:
    """Devuelve resultado de una tool al modelo."""
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output if isinstance(output, str) else json.dumps(output, default=str),
        },
    }


# Tipos de evento de OpenAI cuyos nombres cambiaron entre v1 y v2. Los
# handlers del bridge aceptan ambos para no romper si se cambia el modelo.
AUDIO_DELTA_EVENTS = ("response.audio.delta", "response.output_audio.delta")
ASSISTANT_TRANSCRIPT_DELTA_EVENTS = (
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
)
ASSISTANT_TRANSCRIPT_DONE_EVENTS = (
    "response.audio_transcript.done",
    "response.output_audio_transcript.done",
)
USER_TRANSCRIPT_DONE_EVENTS = (
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.done",
)


def twilio_media_event(stream_sid: str, payload_b64: str) -> str:
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    })


def twilio_clear_event(stream_sid: str) -> str:
    return json.dumps({"event": "clear", "streamSid": stream_sid})


def twilio_mark_event(stream_sid: str, name: str) -> str:
    return json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": name},
    })
