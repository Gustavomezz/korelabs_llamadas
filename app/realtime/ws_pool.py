"""
Pool de WebSockets pre-conectadas al proveedor de Realtime activo.

Una conexión fresca cuesta ~500 ms (TCP+TLS+upgrade). Manteniendo N idle
pre-conectadas saltamos ese costo en cold start de cada llamada.

One-shot: una vez asignada a una llamada, NO regresa al pool — la sesión es
stateful y reusarla mezclaría conversaciones. Se descarta al final.

Provider-agnostic: las WS se abren contra el provider activo (settings).
Si cambias de provider en runtime, hay que recrear el pool — no soportado
hoy (cambio de provider requiere restart del servicio).
"""
import asyncio

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings


class RealtimeWSPool:
    def __init__(self, target_size: int = 2):
        self.target_size = target_size
        self._available: asyncio.Queue[ClientConnection] = asyncio.Queue()
        self._closing = False
        self._refill_inflight = 0
        self._refill_lock = asyncio.Lock()

    async def start(self) -> None:
        # Validamos que el provider activo tenga API key configurada antes
        # de pre-conectar (evita tareas que loopean errores).
        if not _provider_ready():
            logger.warning("ws pool: provider %s sin API key, pool deshabilitado",
                           settings.voice_provider)
            return
        for _ in range(self.target_size):
            asyncio.create_task(self._warm_one())
        logger.info("ws pool: starting target_size=%d provider=%s",
                    self.target_size, settings.voice_provider)

    async def acquire(self) -> ClientConnection:
        while True:
            try:
                ws = self._available.get_nowait()
            except asyncio.QueueEmpty:
                logger.info("ws pool: empty, cold-start fallback")
                self._spawn_refill()
                return await self._open_fresh()

            if getattr(ws, "state", None) and ws.state.name == "OPEN":
                logger.info("ws pool: acquired warm conn (remaining=%d)",
                            self._available.qsize())
                self._spawn_refill()
                return ws
            logger.info("ws pool: discarded stale conn")
            try:
                await ws.close()
            except Exception:
                pass

    async def shutdown(self) -> None:
        self._closing = True
        while not self._available.empty():
            try:
                ws = self._available.get_nowait()
                await ws.close()
            except Exception:
                pass
        logger.info("ws pool: shutdown complete")

    # --- internals ---------------------------------------------------------

    def _spawn_refill(self) -> None:
        if self._closing:
            return
        deficit = self.target_size - self._available.qsize() - self._refill_inflight
        for _ in range(max(deficit, 0)):
            asyncio.create_task(self._warm_one())

    async def _warm_one(self) -> None:
        if self._closing:
            return
        async with self._refill_lock:
            self._refill_inflight += 1
        try:
            ws = await self._open_fresh()
            if self._closing:
                await ws.close()
                return
            await self._available.put(ws)
            logger.info("ws pool: warmed (size=%d)", self._available.qsize())
        except Exception:
            logger.exception("ws pool: warm failed")
        finally:
            async with self._refill_lock:
                self._refill_inflight -= 1

    async def _open_fresh(self) -> ClientConnection:
        # Import local para evitar ciclo (providers importa events)
        from app.realtime.providers import auth_headers, get_provider
        spec = get_provider()
        return await websockets.connect(
            spec.ws_url,
            additional_headers=auth_headers(spec),
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
        )


def _provider_ready() -> bool:
    if settings.voice_provider == "grok":
        return bool(settings.xai_api_key)
    return bool(settings.openai_api_key)


ws_pool: RealtimeWSPool | None = None


def get_pool() -> RealtimeWSPool | None:
    return ws_pool


def init_pool(target_size: int = 2) -> RealtimeWSPool:
    global ws_pool
    ws_pool = RealtimeWSPool(target_size=target_size)
    return ws_pool
