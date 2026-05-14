"""
Helpers puros para construir/parsear los eventos de OpenAI Realtime y Twilio
Media Streams. Sin I/O — fáciles de testear.

OpenAI tiene dos versiones de envelope incompatibles:
- v1 (`gpt-realtime`, `gpt-realtime-1.5`, `gpt-4o-realtime-preview-*`):
  header `OpenAI-Beta: realtime=v1`. Campos planos. Eventos
  `response.audio.delta`, `response.audio_transcript.done`.
- v2 (`gpt-realtime-2` y posteriores): SIN header beta.
  `session.type: "realtime"` requerido. Audio anidado en
  `audio.input/audio.output`. Eventos `response.output_audio.delta`,
  `response.output_audio_transcript.done`. Soporta `reasoning.effort`,
  `semantic_vad`, `noise_reduction`.

Ver memoria openai_realtime_v2_breaking_changes para la tabla completa.
"""
import json
from typing import Any, Iterable


def is_v2_model(model: str) -> bool:
    """Modelos que usan el envelope nuevo de Realtime (session.type='realtime',
    audio.input.turn_detection nested, audio.output.voice nested).

    Incluye:
      - gpt-realtime-mini (cost-efficient, "very fast", default actual)
      - gpt-realtime-2 / gpt-realtime-3 (flagships)
      - cualquier snapshot fechado (ej. gpt-realtime-mini-2025-12-15)
    """
    name = (model or "").lower()
    return (
        name.startswith("gpt-realtime-mini")
        or name.startswith("gpt-realtime-2")
        or name.startswith("gpt-realtime-3")
    )


def _build_turn_detection_v2(vad_type: str, eagerness: str, threshold: float, prefix_ms: int, silence_ms: int) -> dict:
    """semantic_vad: usa modelo de NLU para decidir fin de turno (mejor para
    teléfono, evita barge-in falso). server_vad: VAD basada en energía,
    más rápida pero más ruidosa."""
    if vad_type == "semantic_vad":
        return {
            "type": "semantic_vad",
            "eagerness": eagerness,
            "create_response": True,
            "interrupt_response": True,
        }
    return {
        "type": "server_vad",
        "threshold": threshold,
        "prefix_padding_ms": prefix_ms,
        "silence_duration_ms": silence_ms,
        "create_response": True,
        "interrupt_response": True,
    }


def session_update(
    *,
    instructions: str,
    model: str,
    voice: str = "marin",
    temperature: float = 0.8,
    tools: Iterable[dict] | None = None,
    vad_threshold: float = 0.7,
    vad_silence_ms: int = 300,
    vad_prefix_ms: int = 200,
    reasoning_effort: str = "minimal",
    vad_type: str = "server_vad",
    vad_eagerness: str = "high",
    noise_reduction: str | None = "near_field",
    prompt_id: str | None = None,
    prompt_version: str | None = None,
    max_output_tokens: int | None = None,
) -> dict:
    """
    Construye `session.update` para Realtime. Audio g711 µ-law end-to-end
    (lo que manda Twilio), VAD, transcripción del usuario con whisper-1.
    """
    if is_v2_model(model):
        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcmu"},
            "turn_detection": _build_turn_detection_v2(
                vad_type, vad_eagerness, vad_threshold, vad_prefix_ms, vad_silence_ms,
            ),
            "transcription": {"model": "whisper-1"},
        }
        if noise_reduction:
            # 'near_field' está pensado para mic cercano (auriculares, teléfono).
            # 'far_field' para conferencia con mic en mesa.
            audio_input["noise_reduction"] = {"type": noise_reduction}

        session: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice,
                    "speed": 1.0,
                },
            },
            "tool_choice": "auto",
        }
        # Server-stored prompt es preferido si está disponible (más rápido,
        # mejor cacheado en OpenAI). Sin él caemos a instructions inline.
        if prompt_id:
            prompt_obj: dict[str, Any] = {"id": prompt_id}
            if prompt_version:
                prompt_obj["version"] = prompt_version
            session["prompt"] = prompt_obj
        else:
            session["instructions"] = instructions
        # reasoning.effort solo aplica a modelos GPT-5-class (gpt-realtime-2,
        # gpt-realtime-3). gpt-realtime-mini NO es un reasoning model
        # — la docs oficial no menciona reasoning.effort para mini. Si lo
        # mandamos, puede que sea ignorado o devuelva error.
        is_mini = (model or "").lower().startswith("gpt-realtime-mini")
        if reasoning_effort and not is_mini:
            # minimal | low | medium | high | xhigh. minimal recomendado para
            # voz de baja latencia con tareas simples (calificación, lookup).
            session["reasoning"] = {"effort": reasoning_effort}
        if tools:
            session["tools"] = list(tools)
        if max_output_tokens:
            session["max_output_tokens"] = max_output_tokens
        return {"type": "session.update", "session": session}

    # Envelope v1 (legacy)
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
    if max_output_tokens:
        session["max_response_output_tokens"] = max_output_tokens
    return {"type": "session.update", "session": session}


def initial_response_create(greeting_hint: str | None = None) -> dict:
    payload: dict[str, Any] = {"type": "response.create"}
    if greeting_hint:
        payload["response"] = {"instructions": greeting_hint}
    return payload


def build_greeting_events_openai(greeting_hint: str | None) -> list[dict]:
    """OpenAI v1 y v2 aceptan response.create con instructions directo."""
    return [initial_response_create(greeting_hint)]


def append_audio(payload_b64: str) -> dict:
    return {"type": "input_audio_buffer.append", "audio": payload_b64}


def function_call_output(call_id: str, output: Any) -> dict:
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output if isinstance(output, str) else json.dumps(output, default=str),
        },
    }


# Tipos de evento que cambian de nombre entre v1 y v2; los handlers aceptan ambos.
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
