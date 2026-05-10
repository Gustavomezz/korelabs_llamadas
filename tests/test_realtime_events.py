import json

from app.realtime.events import (
    append_audio,
    function_call_output,
    initial_response_create,
    session_update,
    twilio_clear_event,
    twilio_mark_event,
    twilio_media_event,
)


def test_session_update_uses_g711_ulaw_end_to_end():
    evt = session_update(instructions="hola")
    s = evt["session"]
    assert evt["type"] == "session.update"
    assert s["input_audio_format"] == "g711_ulaw"
    assert s["output_audio_format"] == "g711_ulaw"
    assert s["instructions"] == "hola"
    assert s["voice"] == "cedar"
    assert s["turn_detection"]["type"] == "server_vad"
    assert s["input_audio_transcription"]["model"] == "whisper-1"
    assert "tools" not in s  # vacío -> no se incluye

def test_session_update_with_tools_and_overrides():
    tools = [{"type": "function", "name": "ping", "parameters": {}}]
    evt = session_update(
        instructions="x", voice="marin", temperature=0.6,
        tools=tools, vad_threshold=0.4, vad_silence_ms=400, vad_prefix_ms=200,
    )
    s = evt["session"]
    assert s["voice"] == "marin"
    assert s["temperature"] == 0.6
    assert s["tools"] == tools
    assert s["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.4,
        "prefix_padding_ms": 200,
        "silence_duration_ms": 400,
        "create_response": True,
    }


def test_initial_response_create_with_hint():
    evt = initial_response_create("saluda")
    assert evt["type"] == "response.create"
    assert evt["response"]["instructions"] == "saluda"
    assert "audio" in evt["response"]["modalities"]


def test_initial_response_create_without_hint():
    evt = initial_response_create(None)
    assert evt == {"type": "response.create"}


def test_append_audio_passes_payload_through():
    evt = append_audio("base64data==")
    assert evt == {"type": "input_audio_buffer.append", "audio": "base64data=="}


def test_function_call_output_serializes_dict():
    evt = function_call_output("call_abc", {"slots": [1, 2]})
    assert evt["type"] == "conversation.item.create"
    assert evt["item"]["type"] == "function_call_output"
    assert evt["item"]["call_id"] == "call_abc"
    parsed = json.loads(evt["item"]["output"])
    assert parsed == {"slots": [1, 2]}


def test_function_call_output_passes_string():
    evt = function_call_output("c", "ya hecho")
    assert evt["item"]["output"] == "ya hecho"


def test_twilio_media_event_format():
    msg = twilio_media_event("MZ123", "abc=")
    parsed = json.loads(msg)
    assert parsed == {"event": "media", "streamSid": "MZ123", "media": {"payload": "abc="}}


def test_twilio_clear_and_mark():
    assert json.loads(twilio_clear_event("MZ1")) == {"event": "clear", "streamSid": "MZ1"}
    assert json.loads(twilio_mark_event("MZ1", "x")) == {
        "event": "mark", "streamSid": "MZ1", "mark": {"name": "x"}
    }
