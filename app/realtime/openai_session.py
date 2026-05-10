"""
Cliente WebSocket a un proveedor de Realtime (OpenAI o Grok/xAI).

`open_session` abre la conexión (idealmente del pool pre-warm) y le manda el
session.update y greeting según el provider activo. El bridge consume eventos
después; este módulo no procesa audio.
"""
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings
from app.realtime.events import build_greeting_events_openai, session_update
from app.realtime.providers import (
    ProviderSpec,
    auth_headers,
    build_greeting_events_grok,
    build_session_update_grok,
    get_provider,
)
from app.realtime.ws_pool import get_pool


class RealtimeSession:
    """Wrapper común sobre la WS del provider."""

    def __init__(self, ws: ClientConnection, provider: str):
        self._ws = ws
        self.provider = provider

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def events(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("realtime: non-json frame ignored: %s", raw[:120])

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


# Alias retro-compatible
OpenAISession = RealtimeSession


@asynccontextmanager
async def open_session(
    *,
    instructions: str,
    greeting_hint: str | None,
    voice: str | None = None,
    tools: list[dict] | None = None,
):
    """
    Abre conexión al provider activo (idealmente del pool pre-warm), manda
    session.update + greeting, devuelve sesión lista. Cierra al salir.
    """
    spec = get_provider()
    pool = get_pool()
    t0 = time.monotonic()

    ws: ClientConnection
    if pool is not None:
        try:
            ws = await pool.acquire()
            logger.info("realtime[%s]: pool acquire took %d ms",
                        spec.name, int((time.monotonic() - t0) * 1000))
        except Exception:
            logger.exception("ws pool acquire failed, falling back to cold start")
            ws = await _open_cold(spec)
    else:
        ws = await _open_cold(spec)

    logger.info("realtime[%s]: ws ready (total %d ms)",
                spec.name, int((time.monotonic() - t0) * 1000))
    session = RealtimeSession(ws, provider=spec.name)

    # Construir y enviar session.update + greeting según provider
    update_event, greeting_events, voice_used = _build_session_payloads(
        spec, instructions, greeting_hint, voice, tools or [],
    )
    await session.send(update_event)
    logger.info("realtime[%s]: session.update sent (voice=%s, instructions=%d chars, prompt_id=%s)",
                spec.name, voice_used, len(instructions), settings.openai_prompt_id or "-")
    for evt in greeting_events:
        await session.send(evt)
    logger.info("realtime[%s]: greeting sent (total %d ms since acquire)",
                spec.name, int((time.monotonic() - t0) * 1000))

    try:
        yield session
    finally:
        await session.close()


def _build_session_payloads(
    spec: ProviderSpec,
    instructions: str,
    greeting_hint: str | None,
    voice: str | None,
    tools: list[dict],
) -> tuple[dict, list[dict], str]:
    """Devuelve (session.update, [greeting events], voice usada)."""
    if spec.name == "grok":
        chosen_voice = voice or settings.grok_voice
        update = build_session_update_grok(
            instructions=instructions, voice=chosen_voice, tools=tools or None,
        )
        return update, build_greeting_events_grok(greeting_hint), chosen_voice

    # default: openai (v1 o v2 según modelo)
    chosen_voice = voice or "cedar"
    prompt_id = settings.openai_prompt_id or None
    update = session_update(
        instructions=instructions,
        model=settings.openai_realtime_model,
        voice=chosen_voice,
        tools=tools or [],
        reasoning_effort=settings.openai_reasoning_effort,
        vad_type=settings.realtime_vad_type,
        vad_eagerness=settings.realtime_vad_eagerness,
        vad_threshold=settings.realtime_vad_threshold,
        vad_silence_ms=settings.realtime_vad_silence_ms,
        vad_prefix_ms=settings.realtime_vad_prefix_ms,
        prompt_id=prompt_id,
        max_output_tokens=settings.openai_max_output_tokens,
    )
    return update, build_greeting_events_openai(greeting_hint), chosen_voice


async def _open_cold(spec: ProviderSpec) -> ClientConnection:
    """Conexión fresca (sin pool). Ruta de fallback."""
    logger.info("realtime[%s]: cold-start connect", spec.name)
    return await websockets.connect(
        spec.ws_url,
        additional_headers=auth_headers(spec),
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=10,
    )
