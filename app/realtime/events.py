"""
Helpers puros para construir/parsear los eventos de OpenAI Realtime y Twilio
Media Streams. Sin I/O — fáciles de testear.

Referencia OpenAI:  https://platform.openai.com/docs/guides/realtime
Referencia Twilio:  https://www.twilio.com/docs/voice/twiml/stream
"""
import json
from typing import Any, Iterable


def session_update(
    *,
    instructions: str,
    voice: str = "cedar",
    temperature: float = 0.8,
    tools: Iterable[dict] | None = None,
    vad_threshold: float = 0.5,
    vad_silence_ms: int = 600,
    vad_prefix_ms: int = 300,
) -> dict:
    """
    Configura la sesión de Realtime. Audio g711_ulaw end-to-end (lo que manda
    Twilio Media Streams), VAD server-side, transcripción del usuario con
    whisper-1 para alimentar `call_transcripts`.
    """
    session: dict[str, Any] = {
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
        payload["response"] = {"instructions": greeting_hint, "modalities": ["audio", "text"]}
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


def twilio_media_event(stream_sid: str, payload_b64: str) -> str:
    """
    Frame de salida hacia Twilio. Twilio espera el JSON serializado por el WS.
    Si `streamSid` no coincide con el que mandó en `start`, Twilio descarta.
    """
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    })


def twilio_clear_event(stream_sid: str) -> str:
    """
    Limpia cualquier audio ya bufferado del lado del cliente Twilio. Lo usamos
    cuando OpenAI detecta que el caller empezó a hablar (barge-in), para cortar
    inmediatamente la voz del bot.
    """
    return json.dumps({"event": "clear", "streamSid": stream_sid})


def twilio_mark_event(stream_sid: str, name: str) -> str:
    return json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": name},
    })
