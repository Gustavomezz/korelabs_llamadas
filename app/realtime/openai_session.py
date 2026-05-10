"""
Cliente WebSocket a OpenAI Realtime API.

Auth: solo `Authorization: Bearer ...` para v2; para v1 también
`OpenAI-Beta: realtime=v1`. Detección por nombre del modelo.

Si el pool de WS pre-warm tiene conexiones disponibles, las usamos para
saltar el handshake (~500ms). Caemos a cold start como fallback.
"""
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings
from app.realtime.events import initial_response_create, is_v2_model, session_update
from app.realtime.ws_pool import get_pool

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
    Abre conexión a OpenAI Realtime (idealmente del pool pre-warm),
    manda session.update + response.create inicial, devuelve sesión lista.
    Cierra al salir.
    """
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    model = settings.openai_realtime_model
    pool = get_pool()
    t0 = time.monotonic()

    ws: ClientConnection
    if pool is not None:
        try:
            ws = await pool.acquire()
            logger.info("openai realtime: pool acquire took %d ms", int((time.monotonic() - t0) * 1000))
        except Exception:
            logger.exception("ws pool acquire failed, falling back to cold start")
            ws = await _open_cold(model)
    else:
        ws = await _open_cold(model)

    logger.info("openai realtime: ws ready (total %d ms)", int((time.monotonic() - t0) * 1000))
    session = OpenAISession(ws)
    prompt_id = settings.openai_prompt_id or None
    await session.send(session_update(
        instructions=instructions,
        model=model,
        voice=voice,
        tools=tools or [],
        reasoning_effort=settings.openai_reasoning_effort,
        vad_type=settings.realtime_vad_type,
        vad_eagerness=settings.realtime_vad_eagerness,
        prompt_id=prompt_id,
    ))
    if prompt_id:
        logger.info("openai realtime: session.update sent (prompt_id=%s, voice=%s)", prompt_id, voice)
    else:
        logger.info("openai realtime: session.update sent (instructions=%d chars, voice=%s)", len(instructions), voice)
    await session.send(initial_response_create(greeting_hint))
    logger.info("openai realtime: greeting response.create sent (total %d ms since acquire)",
                int((time.monotonic() - t0) * 1000))
    try:
        yield session
    finally:
        await session.close()


async def _open_cold(model: str) -> ClientConnection:
    """Conexión fresca (sin pool). Ruta de fallback."""
    url = OPENAI_REALTIME_URL_TEMPLATE.format(model=model)
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    if not is_v2_model(model):
        headers["OpenAI-Beta"] = "realtime=v1"
    logger.info("openai realtime: cold-start connect model=%s v2=%s", model, is_v2_model(model))
    return await websockets.connect(
        url,
        additional_headers=headers,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=10,
    )
