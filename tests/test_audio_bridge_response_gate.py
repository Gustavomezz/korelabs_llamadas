import pytest

from app.realtime.audio_bridge import AudioBridge


class FakeOpenAI:
    def __init__(self):
        self.sent = []

    async def send(self, event):
        self.sent.append(event)


def make_bridge(response_active=False):
    bridge = AudioBridge.__new__(AudioBridge)
    bridge.openai = FakeOpenAI()
    bridge.call_id = 123
    bridge._response_active = response_active
    bridge._responded_user_items = set()
    return bridge


@pytest.mark.asyncio
async def test_create_response_after_user_transcript():
    bridge = make_bridge()

    await bridge._create_response_after_user_transcript(
        {"item_id": "item_1"},
        "quiero agendar una cita",
    )

    assert bridge.openai.sent == [{"type": "response.create"}]


@pytest.mark.asyncio
async def test_duplicate_user_transcript_does_not_create_second_response():
    bridge = make_bridge()

    await bridge._create_response_after_user_transcript({"item_id": "item_1"}, "sí")
    await bridge._create_response_after_user_transcript({"item_id": "item_1"}, "sí")

    assert bridge.openai.sent == [{"type": "response.create"}]


@pytest.mark.asyncio
async def test_user_transcript_does_not_interrupt_active_response():
    bridge = make_bridge(response_active=True)

    await bridge._create_response_after_user_transcript({"item_id": "item_1"}, "hola")

    assert bridge.openai.sent == []
