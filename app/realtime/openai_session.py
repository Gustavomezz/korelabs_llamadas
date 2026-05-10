"""
Cliente WebSocket a OpenAI Realtime API.

Auth: solo `Authorization: Bearer ...`. Para gpt-realtime-2 NO se manda
`OpenAI-Beta: realtime=v1` (rompe el handshake con `invalid_model`). Para
modelos legacy v1 sí se manda — events.is_v2_model decide.
"""
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings
from app.realtime.events import initial_response_create, is_v2_model, session_update

OPENAI_REALTIME_URL_TEMPLATE = "wss://api.openai.com/v1/realtime?model={model}"


class OpenAISession:
    def __init__(self, ws: ClientConnection):
        self._ws = ws

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("openai realtime: non-json frame ignored: %s", raw[:120])

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


@asynccontextmanager
async def open_session(
    *,
    instructions: str,
    greeting_hint: str | None,
    voice: str = "cedar",
    tools: list[dict] | None = None,
):
    """
    Abre conexión a OpenAI Realtime, manda session.update + response.create
    inicial, y devuelve `OpenAISession` lista para hacer pump. Cierra al salir.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    model = settings.openai_realtime_model
    url = OPENAI_REALTIME_URL_TEMPLATE.format(model=model)
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    # El header beta es requerido por v1 y prohibido por v2. Sin `realtime=v1`
    # los modelos viejos rechazan; CON ese header los nuevos rechazan.
    if not is_v2_model(model):
        headers["OpenAI-Beta"] = "realtime=v1"

    logger.info("openai realtime: connecting model=%s v2=%s", model, is_v2_model(model))
    async with websockets.connect(
        url,
        additional_headers=headers,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=10,
    ) as ws:
        logger.info("openai realtime: ws connected")
        session = OpenAISession(ws)
        await session.send(session_update(
            instructions=instructions,
            model=model,
            voice=voice,
            tools=tools or [],
        ))
        logger.info("openai realtime: session.update sent (instructions=%d chars, voice=%s)", len(instructions), voice)
        await session.send(initial_response_create(greeting_hint))
        logger.info("openai realtime: greeting response.create sent")
        try:
            yield session
        finally:
            await session.close()
