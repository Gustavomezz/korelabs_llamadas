"""
Bridge bidireccional Twilio Media Streams <-> OpenAI Realtime.

Dos tareas asyncio corren en paralelo:
  - `_pump_twilio_to_openai`: lee eventos `media` de Twilio, los inyecta como
    `input_audio_buffer.append` en OpenAI.
  - `_pump_openai_to_twilio`: lee eventos de OpenAI, escribe `media` events a
    Twilio cuando llegan deltas de audio, persiste transcripts en BD, y
    despacha tool calls.

Cuando cualquiera de las dos tareas termina (caller cuelga, OpenAI cierra,
error fatal), la otra se cancela y `run` retorna.
"""
import asyncio
import json

import asyncpg
from fastapi import WebSocket
from fastapi.websockets import WebSocketState

from app.ai.tools import ToolContext, execute_tool
from app.config import logger
from app.models.calls import insert_transcript
from app.realtime.events import (
    append_audio,
    function_call_output,
    twilio_clear_event,
    twilio_media_event,
)
from app.realtime.openai_session import OpenAISession


class AudioBridge:
    def __init__(
        self,
        *,
        twilio_ws: WebSocket,
        openai: OpenAISession,
        pool: asyncpg.Pool,
        stream_sid: str,
        call_id: int,
        wa_id: str,
    ):
        self.twilio = twilio_ws
        self.openai = openai
        self.pool = pool
        self.stream_sid = stream_sid
        self.call_id = call_id
        self.tool_ctx = ToolContext(pool=pool, wa_id=wa_id)
        self._frames_in = 0
        self._frames_out = 0
        # Sólo limpiamos el buffer de Twilio (barge-in) si el bot está
        # actualmente generando una respuesta. Sin este guard, cualquier
        # falso speech_started del caller cortaría audio que ni siquiera
        # ha empezado a enviarse.
        self._response_active = False
        self._first_audio_delta_logged = False

    async def run(self) -> None:
        t1 = asyncio.create_task(self._pump_twilio_to_openai(), name="twilio->openai")
        t2 = asyncio.create_task(self._pump_openai_to_twilio(), name="openai->twilio")
        logger.info("bridge started call_id=%s wa_id=%s", self.call_id, self.tool_ctx.wa_id)
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done | pending:
            try:
                await task
            except (asyncio.CancelledError, Exception) as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    logger.warning("bridge task %s ended with: %r", task.get_name(), exc)
        logger.info(
            "bridge closed call_id=%s frames_in=%s frames_out=%s",
            self.call_id, self._frames_in, self._frames_out,
        )

    # --- pumps -------------------------------------------------------------

    async def _pump_twilio_to_openai(self) -> None:
        try:
            while True:
                raw = await self.twilio.receive_text()
                event = json.loads(raw)
                kind = event.get("event")
                if kind == "media":
                    payload = event["media"]["payload"]
                    await self.openai.send(append_audio(payload))
                    self._frames_in += 1
                elif kind == "stop":
                    logger.info("twilio sent stop call_id=%s", self.call_id)
                    return
        except Exception as exc:
            from starlette.websockets import WebSocketDisconnect
            if isinstance(exc, WebSocketDisconnect):
                logger.info("twilio ws disconnected call_id=%s", self.call_id)
            else:
                logger.exception("twilio pump error call_id=%s", self.call_id)
            return

    async def _pump_openai_to_twilio(self) -> None:
        event_count = 0
        try:
            async for event in self.openai.events():
                event_count += 1
                kind = event.get("type", "?")

                if event_count == 1:
                    logger.info("openai first event call_id=%s type=%s", self.call_id, kind)

                if kind == "response.audio.delta":
                    delta = event.get("delta")
                    if delta:
                        if not self._first_audio_delta_logged:
                            logger.info(
                                "openai first audio.delta call_id=%s size=%d twilio_state=%s",
                                self.call_id, len(delta), self.twilio.application_state.name,
                            )
                            self._first_audio_delta_logged = True
                        await self._send_twilio(twilio_media_event(self.stream_sid, delta))
                        self._frames_out += 1
                elif kind == "response.created":
                    self._response_active = True
                elif kind == "response.done":
                    self._response_active = False
                elif kind == "input_audio_buffer.speech_started":
                    if self._response_active:
                        logger.info("barge-in: clearing twilio buffer call_id=%s", self.call_id)
                        await self._send_twilio(twilio_clear_event(self.stream_sid))
                elif kind == "response.audio_transcript.done":
                    transcript = (event.get("transcript") or "").strip()
                    if transcript:
                        logger.info("assistant said call_id=%s: %s", self.call_id, transcript[:120])
                        await self._save_transcript("assistant", transcript)
                elif kind == "conversation.item.input_audio_transcription.completed":
                    transcript = (event.get("transcript") or "").strip()
                    if transcript:
                        logger.info("user said call_id=%s: %s", self.call_id, transcript[:120])
                        await self._save_transcript("user", transcript)
                elif kind == "response.function_call_arguments.done":
                    asyncio.create_task(self._handle_tool_call(event))
                elif kind == "error":
                    err = event.get("error", {})
                    logger.error(
                        "openai realtime error call_id=%s code=%s message=%s",
                        self.call_id, err.get("code"), err.get("message"),
                    )
        except Exception:
            logger.exception("openai pump error call_id=%s events_seen=%d", self.call_id, event_count)
            return
        logger.info("openai pump exited normally call_id=%s events_seen=%d", self.call_id, event_count)

    # --- tool dispatch -----------------------------------------------------

    async def _handle_tool_call(self, event: dict) -> None:
        """
        Ejecuta la tool en background y devuelve el resultado a OpenAI.
        Lanzado como task para no bloquear el pump principal mientras
        Google Calendar responde (1-2s típicos).
        """
        name = event.get("name") or "?"
        fn_call_id = event.get("call_id")
        raw_args = event.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            logger.warning("tool %s got non-json arguments: %r", name, raw_args)
            args = {}

        logger.info("tool call name=%s args=%s call_id=%s", name, args, self.call_id)
        result_str = await execute_tool(name, args, self.tool_ctx)

        # Persistir como una sola fila en call_transcripts para auditoría.
        try:
            parsed_result = json.loads(result_str)
        except json.JSONDecodeError:
            parsed_result = {"raw": result_str}
        try:
            await insert_transcript(
                self.pool,
                call_id=self.call_id,
                role="tool",
                content=name,
                tool_name=name,
                tool_args=args,
                tool_result=parsed_result,
            )
        except Exception:
            logger.exception("failed to persist tool transcript call_id=%s", self.call_id)

        if not fn_call_id:
            logger.warning("tool call %s without call_id, can't return output", name)
            return

        await self.openai.send(function_call_output(fn_call_id, result_str))
        # Pedir al modelo que continúe con el output recién entregado.
        await self.openai.send({"type": "response.create"})

    # --- helpers -----------------------------------------------------------

    async def _send_twilio(self, message: str) -> None:
        if self.twilio.application_state != WebSocketState.CONNECTED:
            logger.warning(
                "drop send to twilio call_id=%s state=%s",
                self.call_id, self.twilio.application_state.name,
            )
            return
        try:
            await self.twilio.send_text(message)
        except Exception:
            logger.exception("failed to send to twilio call_id=%s", self.call_id)

    async def _save_transcript(self, role: str, content: str) -> None:
        try:
            await insert_transcript(
                self.pool, call_id=self.call_id, role=role, content=content,
            )
        except Exception:
            logger.exception("failed to persist transcript call_id=%s role=%s", self.call_id, role)
