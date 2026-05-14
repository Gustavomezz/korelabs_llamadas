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
import time

import asyncpg
from fastapi import WebSocket
from fastapi.websockets import WebSocketState

from app.ai.tools import ToolContext, execute_tool
from app.config import logger, settings
from app.models.calls import insert_transcript
from app.realtime.events import (
    ASSISTANT_TRANSCRIPT_DELTA_EVENTS,
    ASSISTANT_TRANSCRIPT_DONE_EVENTS,
    AUDIO_DELTA_EVENTS,
    USER_TRANSCRIPT_DONE_EVENTS,
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
        # ms desde que la response actual empezó a producir audio. Usamos esto
        # para suprimir barge-in muy temprano (probable eco del propio bot
        # rebotando en la línea telefónica), guard configurable via
        # settings.barge_in_guard_ms.
        self._response_audio_started_at: float | None = None
        # Para medir latencia turn-by-turn REAL: timestamp del último
        # speech_stopped del usuario. Comparado contra el siguiente
        # response.created (= cuándo OpenAI decidió empezar a generar) y
        # contra la primera audio.delta de esa response (= primer byte audible
        # listo para mandar a Twilio).
        self._last_user_speech_stopped_at: float | None = None
        # Acumulador de transcript del assistant. En gpt-realtime-2 el `.done`
        # tarda o no llega; acumulamos deltas y flusheamos en `.done` o en
        # `response.done` (lo que llegue primero). Indexed por response_id.
        self._assistant_transcript_buf: dict[str, str] = {}
        # item_id del mensaje actual del assistant. Lo capturamos del primer
        # audio.delta de cada response para poder mandar
        # conversation.item.truncate al hacer barge-in (limpiar Twilio NO
        # detiene al modelo — el truncate sí). Reset en response.done.
        self._last_assistant_item_id: str | None = None
        # Timestamp del último response.done. Usado para suprimir VAD
        # speech_started que dispare en los `post_speech_guard_ms` siguientes
        # — eso es típicamente reverb del speaker, no usuario real.
        self._last_response_done_at: float | None = None

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
                    # HALF-DUPLEX: si está activo y el bot está hablando,
                    # descartamos el frame en lugar de mandarlo al server.
                    # Esto garantiza que la VAD del server nunca vea el eco
                    # del propio bot rebotando por el altavoz.
                    if settings.half_duplex_mode and self._response_active:
                        self._frames_in += 1
                        continue
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

                # Debug del prompt resuelto: session.created/updated echo back
                # el estado completo. Útil para confirmar si el stored prompt
                # de OpenAI realmente cargó el contenido que esperamos.
                if kind in ("session.created", "session.updated"):
                    sess = event.get("session") or {}
                    instr = sess.get("instructions") or ""
                    prompt_obj = sess.get("prompt") or {}
                    logger.info(
                        "session %s: instructions_len=%d prompt_obj=%s first200=%r",
                        kind.split(".")[1],
                        len(instr),
                        prompt_obj,
                        instr[:200],
                    )

                if kind in AUDIO_DELTA_EVENTS:
                    delta = event.get("delta")
                    if delta:
                        if not self._first_audio_delta_logged:
                            logger.info(
                                "openai first audio.delta call_id=%s type=%s size=%d twilio_state=%s",
                                self.call_id, kind, len(delta), self.twilio.application_state.name,
                            )
                            self._first_audio_delta_logged = True
                        # Capturar item_id de cada respuesta (lo necesitamos
                        # para conversation.item.truncate en barge-in).
                        item_id = event.get("item_id")
                        if item_id and item_id != self._last_assistant_item_id:
                            self._last_assistant_item_id = item_id
                        if self._response_audio_started_at is None:
                            self._response_audio_started_at = time.monotonic()
                            # Latencia REAL turn-by-turn: tiempo desde que el
                            # caller dejó de hablar hasta el primer byte de
                            # audio del bot. Lo que el usuario percibe como
                            # "responde rápido / lento".
                            if self._last_user_speech_stopped_at:
                                ms = int((time.monotonic() - self._last_user_speech_stopped_at) * 1000)
                                logger.info(
                                    "TURN LATENCY call_id=%s speech_stopped→first_audio=%d ms",
                                    self.call_id, ms,
                                )
                                self._last_user_speech_stopped_at = None
                        await self._send_twilio(twilio_media_event(self.stream_sid, delta))
                        self._frames_out += 1
                elif kind == "input_audio_buffer.speech_stopped":
                    self._last_user_speech_stopped_at = time.monotonic()
                    logger.debug("vad: user speech_stopped call_id=%s", self.call_id)
                elif kind == "response.created":
                    self._response_active = True
                    self._response_audio_started_at = None
                    rid = (event.get("response") or {}).get("id")
                    if rid:
                        self._assistant_transcript_buf[rid] = ""
                elif kind == "response.done":
                    self._response_active = False
                    self._response_audio_started_at = None
                    self._last_assistant_item_id = None
                    # Marca para el post-speech guard: ignora speech_started
                    # del server VAD durante los próximos `post_speech_guard_ms`
                    # ms (es reverb del speaker, no caller real).
                    self._last_response_done_at = time.monotonic()
                    resp = event.get("response") or {}
                    rid = resp.get("id")
                    # Loguea métricas de prompt caching para validar que el
                    # cache hit pasa entre llamadas. cached_tokens > 0 =
                    # OpenAI reusó el prompt en lugar de re-procesarlo.
                    usage = resp.get("usage") or {}
                    cached = (usage.get("input_token_details") or {}).get("cached_tokens", 0)
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    if in_tok or out_tok:
                        cache_pct = (cached / in_tok * 100) if in_tok else 0
                        logger.info(
                            "openai usage call_id=%s in=%d cached=%d (%.0f%%) out=%d",
                            self.call_id, in_tok, cached, cache_pct, out_tok,
                        )
                    await self._flush_assistant_transcript(rid)
                elif kind == "input_audio_buffer.speech_started":
                    # Caller empezó a hablar.
                    # Si el bot está hablando: barge-in. Sólo respetamos si
                    # ya pasaron `barge_in_guard_ms` desde el primer audio
                    # enviado, para no confundir eco inmediato del bot con
                    # speech del caller.
                    if not self._response_active:
                        # Post-speech guard: si la VAD disparó muy cerca del
                        # último response.done, es reverb/eco del speaker.
                        # Limpiar el buffer de audio del server para que NO
                        # cree un mensaje fantasma de usuario.
                        if (
                            self._last_response_done_at is not None
                            and (time.monotonic() - self._last_response_done_at) * 1000
                                < settings.post_speech_guard_ms
                        ):
                            elapsed = int(
                                (time.monotonic() - self._last_response_done_at) * 1000
                            )
                            logger.info(
                                "vad: post-speech echo SUPPRESSED (within %dms guard): elapsed=%dms call_id=%s",
                                settings.post_speech_guard_ms, elapsed, self.call_id,
                            )
                            # Pide al server descartar el audio bufferizado
                            # para que no genere transcript fantasma.
                            await self.openai.send({"type": "input_audio_buffer.clear"})
                            continue
                        logger.info("vad: user speech_started (bot idle) call_id=%s", self.call_id)
                    elif (
                        self._response_audio_started_at is None
                        or (time.monotonic() - self._response_audio_started_at) * 1000 < settings.barge_in_guard_ms
                    ):
                        elapsed = (
                            int((time.monotonic() - self._response_audio_started_at) * 1000)
                            if self._response_audio_started_at else 0
                        )
                        logger.info(
                            "barge-in SUPPRESSED (within guard %dms): elapsed=%dms call_id=%s",
                            settings.barge_in_guard_ms, elapsed, self.call_id,
                        )
                    else:
                        # Barge-in real. CUATRO acciones para que sea
                        # fulminante (frene-en-seco):
                        # 1. response.cancel: mata la generación server-side
                        #    inmediatamente. Sin esto, el modelo sigue
                        #    generando tokens detrás de bambalinas y puede
                        #    seguir mandando audio hasta que llegue el
                        #    response.done natural.
                        # 2. conversation.item.truncate: marca el punto exacto
                        #    donde se cortó para que el contexto refleje lo
                        #    que el caller realmente alcanzó a oír (sin esto
                        #    el bot cree que dijo más de lo que el usuario
                        #    escuchó).
                        # 3. twilio clear: vacía el buffer de playback de
                        #    Twilio para que el audio en vuelo deje de oírse.
                        # 4. Marcar _response_active=False y reset del timer
                        #    inmediatamente para que un segundo intento de
                        #    interrupción NO se ignore mientras llega el
                        #    response.done del server (50-100ms de delay).
                        elapsed_ms = (
                            int((time.monotonic() - self._response_audio_started_at) * 1000)
                            if self._response_audio_started_at
                            else 0
                        )
                        # 1. Cancel (lo más importante para frenar el modelo)
                        await self.openai.send({"type": "response.cancel"})
                        # 2. Truncate (deja el contexto consistente)
                        if self._last_assistant_item_id:
                            await self.openai.send({
                                "type": "conversation.item.truncate",
                                "item_id": self._last_assistant_item_id,
                                "content_index": 0,
                                "audio_end_ms": elapsed_ms,
                            })
                        # 3. Vaciar buffer de Twilio
                        await self._send_twilio(twilio_clear_event(self.stream_sid))
                        # 4. Resetear flags localmente (no esperar response.done)
                        self._response_active = False
                        self._response_audio_started_at = None
                        logger.info(
                            "barge-in HARD: cancel+truncate+clear at %d ms item=%s call_id=%s",
                            elapsed_ms, self._last_assistant_item_id or "<none>", self.call_id,
                        )
                elif kind in ASSISTANT_TRANSCRIPT_DELTA_EVENTS:
                    rid = event.get("response_id") or ""
                    delta = event.get("delta") or ""
                    if delta:
                        self._assistant_transcript_buf[rid] = (
                            self._assistant_transcript_buf.get(rid, "") + delta
                        )
                elif kind in ASSISTANT_TRANSCRIPT_DONE_EVENTS:
                    rid = event.get("response_id") or ""
                    final = (event.get("transcript") or self._assistant_transcript_buf.get(rid, "")).strip()
                    if final:
                        logger.info("assistant said call_id=%s: %s", self.call_id, final[:120])
                        await self._save_transcript("assistant", final)
                    self._assistant_transcript_buf.pop(rid, None)
                elif kind in USER_TRANSCRIPT_DONE_EVENTS:
                    transcript = (event.get("transcript") or "").strip()
                    if transcript:
                        logger.info("user said call_id=%s: %s", self.call_id, transcript[:120])
                        await self._save_transcript("user", transcript)
                elif kind == "response.function_call_arguments.done":
                    asyncio.create_task(self._handle_tool_call(event))
                elif kind == "error":
                    err = event.get("error", {})
                    logger.error(
                        "openai realtime error call_id=%s type=%s code=%s param=%s message=%s event_id=%s",
                        self.call_id,
                        err.get("type"),
                        err.get("code"),
                        err.get("param"),
                        err.get("message"),
                        event.get("event_id"),
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

    async def _flush_assistant_transcript(self, response_id: str | None) -> None:
        """
        Persistir y borrar el buffer del assistant para un response_id dado.
        Se invoca desde response.done como fallback si nunca llegó el evento
        `.done` específico del transcript.
        """
        if not response_id:
            return
        text = (self._assistant_transcript_buf.pop(response_id, "") or "").strip()
        if text:
            logger.info("assistant said (flush) call_id=%s: %s", self.call_id, text[:120])
            await self._save_transcript("assistant", text)

    async def _save_transcript(self, role: str, content: str) -> None:
        try:
            await insert_transcript(
                self.pool, call_id=self.call_id, role=role, content=content,
            )
        except Exception:
            logger.exception("failed to persist transcript call_id=%s role=%s", self.call_id, role)
