import json

from app.realtime.events import (
    append_audio,
    function_call_output,
    initial_response_create,
    is_v2_model,
    session_update,
    twilio_clear_event,
    twilio_mark_event,
    twilio_media_event,
)


# ---------- detector v1/v2 ----------

def test_is_v2_model_recognizes_v2_family():
    assert is_v2_model("gpt-realtime-2") is True
    assert is_v2_model("gpt-realtime-2.5") is True
    assert is_v2_model("gpt-realtime-2-2026-05-07") is True
    assert is_v2_model("gpt-realtime-3") is True


def test_is_v2_model_treats_legacy_as_v1():
    assert is_v2_model("gpt-realtime") is False
    assert is_v2_model("gpt-realtime-1.5") is False
    assert is_v2_model("gpt-realtime-mini") is False
    assert is_v2_model("gpt-4o-realtime-preview") is False
    assert is_v2_model("gpt-4o-realtime-preview-2024-12-17") is False
    assert is_v2_model("") is False


# ---------- session_update v2 ----------

def test_session_update_v2_envelope_defaults():
    evt = session_update(instructions="hola", model="gpt-realtime-2")
    s = evt["session"]
    assert evt["type"] == "session.update"
    assert s["type"] == "realtime"
    assert s["output_modalities"] == ["audio"]
    assert s["instructions"] == "hola"
    # Audio anidado
    assert s["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert s["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert s["audio"]["output"]["voice"] == "cedar"
    assert s["audio"]["input"]["transcription"] == {"model": "whisper-1"}
    # Default ahora server_vad con threshold alto + silence corto (latencia mínima).
    td = s["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.85
    assert td["silence_duration_ms"] == 300
    assert td["prefix_padding_ms"] == 200
    assert td["interrupt_response"] is True
    # noise_reduction near_field por defecto (anti-eco línea telefónica)
    assert s["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
    # Default reasoning_effort 'minimal' (latencia mínima)
    assert s["reasoning"] == {"effort": "minimal"}
    # NO debe haber campos del envelope viejo
    assert "modalities" not in s
    assert "input_audio_format" not in s
    assert "output_audio_format" not in s


def test_session_update_v2_can_use_semantic_vad_explicit():
    evt = session_update(
        instructions="x", model="gpt-realtime-2",
        vad_type="semantic_vad", vad_eagerness="high",
    )
    td = evt["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "semantic_vad"
    assert td["eagerness"] == "high"


def test_session_update_v2_aggressive_vad_for_low_latency():
    """Configuración recomendada para latencia mínima turn-by-turn."""
    evt = session_update(
        instructions="x", model="gpt-realtime-2",
        vad_type="server_vad", vad_threshold=0.85, vad_silence_ms=300, vad_prefix_ms=200,
    )
    td = evt["session"]["audio"]["input"]["turn_detection"]
    assert td["type"] == "server_vad"
    assert td["threshold"] == 0.85
    assert td["silence_duration_ms"] == 300
    assert td["prefix_padding_ms"] == 200


def test_session_update_v2_can_disable_noise_reduction():
    evt = session_update(instructions="x", model="gpt-realtime-2", noise_reduction=None)
    assert "noise_reduction" not in evt["session"]["audio"]["input"]


def test_session_update_v2_can_change_reasoning_effort():
    evt = session_update(instructions="x", model="gpt-realtime-2", reasoning_effort="medium")
    assert evt["session"]["reasoning"]["effort"] == "medium"


def test_session_update_v2_uses_prompt_id_when_set():
    """Si pasamos prompt_id, debe usarse en lugar de instructions inline.
    Reduce payload del session.update y maximiza cache hit en OpenAI."""
    evt = session_update(
        instructions="esto NO debería enviarse",
        model="gpt-realtime-2",
        prompt_id="pmpt_abc123",
    )
    s = evt["session"]
    assert s["prompt"] == {"id": "pmpt_abc123"}
    assert "instructions" not in s


def test_session_update_v2_prompt_id_with_version():
    evt = session_update(
        instructions="x",
        model="gpt-realtime-2",
        prompt_id="pmpt_xyz",
        prompt_version="3",
    )
    assert evt["session"]["prompt"] == {"id": "pmpt_xyz", "version": "3"}


def test_session_update_v2_falls_back_to_instructions_without_prompt_id():
    evt = session_update(instructions="hola amigo", model="gpt-realtime-2")
    assert evt["session"]["instructions"] == "hola amigo"
    assert "prompt" not in evt["session"]


def test_session_update_v2_with_tools():
    tools = [{"type": "function", "name": "ping", "parameters": {}}]
    evt = session_update(
        instructions="x", model="gpt-realtime-2",
        tools=tools, voice="marin", reasoning_effort="medium",
    )
    s = evt["session"]
    assert s["audio"]["output"]["voice"] == "marin"
    assert s["tools"] == tools
    assert s["reasoning"]["effort"] == "medium"


# ---------- session_update v1 (legacy) ----------

def test_session_update_v1_envelope():
    evt = session_update(instructions="hola", model="gpt-realtime")
    s = evt["session"]
    assert evt["type"] == "session.update"
    assert "type" not in s  # v1 NO tiene session.type
    assert s["modalities"] == ["audio", "text"]
    assert s["voice"] == "cedar"
    assert s["input_audio_format"] == "g711_ulaw"
    assert s["output_audio_format"] == "g711_ulaw"
    assert s["input_audio_transcription"] == {"model": "whisper-1"}
    assert s["turn_detection"]["type"] == "server_vad"
    assert s["turn_detection"]["threshold"] == 0.85
    assert "audio" not in s
    assert "output_modalities" not in s


def test_session_update_v1_with_tools():
    tools = [{"type": "function", "name": "ping", "parameters": {}}]
    evt = session_update(
        instructions="x", model="gpt-realtime", voice="marin",
        temperature=0.6, tools=tools,
        vad_threshold=0.4, vad_silence_ms=400, vad_prefix_ms=200,
    )
    s = evt["session"]
    assert s["voice"] == "marin"
    assert s["temperature"] == 0.6
    assert s["tools"] == tools
    assert s["turn_detection"]["threshold"] == 0.4
    assert s["turn_detection"]["silence_duration_ms"] == 400
    assert s["turn_detection"]["prefix_padding_ms"] == 200


# ---------- otros helpers (estables entre versiones) ----------

def test_initial_response_create_with_hint():
    evt = initial_response_create("saluda")
    assert evt["type"] == "response.create"
    assert evt["response"]["instructions"] == "saluda"


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
    assert json.loads(evt["item"]["output"]) == {"slots": [1, 2]}


def test_function_call_output_passes_string():
    evt = function_call_output("c", "ya hecho")
    assert evt["item"]["output"] == "ya hecho"


def test_twilio_media_event_format():
    msg = twilio_media_event("MZ123", "abc=")
    assert json.loads(msg) == {"event": "media", "streamSid": "MZ123", "media": {"payload": "abc="}}


def test_twilio_clear_and_mark():
    assert json.loads(twilio_clear_event("MZ1")) == {"event": "clear", "streamSid": "MZ1"}
    assert json.loads(twilio_mark_event("MZ1", "x")) == {
        "event": "mark", "streamSid": "MZ1", "mark": {"name": "x"}
    }
