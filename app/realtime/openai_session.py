"""
Cliente WebSocket a OpenAI Realtime API.

Responsabilidades:
- Abrir la WS con el header de auth.
- Mandar `session.update` inicial + `response.create` para que el bot salude.
- Exponer `send(event_dict)` y `events()` (async iterator) para que el bridge
  haga el pump.
- Cerrar limpio al salir.

NO toca audio de Twilio ni la BD: eso es responsabilidad del bridge. Esta
clase es estricta pump de eventos de OpenAI.
"""
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings
from app.realtime.events import initial_response_create, session_update

OPENAI_REALTIME_URL_TEMPLATE = "wss://api.openai.com/v1/realtime?model={model}"


class OpenAISession:
    def __init__(self, ws: ClientConnection):
        self._ws = ws

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                # Realtime no manda binarios pero por defensa lo decodificamos.
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

    url = OPENAI_REALTIME_URL_TEMPLATE.format(model=settings.openai_realtime_model)
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "OpenAI-Beta": "realtime=v1",
    }

    logger.info("openai realtime: connecting model=%s", settings.openai_realtime_model)
    # max_size=None: los frames de audio en base64 pueden ser grandes; sin tope
    # el cliente no rechaza por tamaño y mantenemos latencia baja.
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
        await session.send(session_update(instructions=instructions, voice=voice, tools=tools or []))
        logger.info("openai realtime: session.update sent (instructions=%d chars, voice=%s)", len(instructions), voice)
        await session.send(initial_response_create(greeting_hint))
        logger.info("openai realtime: greeting response.create sent")
        try:
            yield session
        finally:
            await session.close()
