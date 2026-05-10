"""
Pool de WebSockets pre-conectadas a OpenAI Realtime.

Una conexión fresca a `wss://api.openai.com/v1/realtime` cuesta ~500 ms
(TCP + TLS handshake + WebSocket upgrade). Si abrimos esa WS en cold start
de cada llamada, ese tiempo se suma al cold start total. Manteniendo N
conexiones idle pre-conectadas, al llegar la llamada solo agarramos una
del pool y vamos directo a `session.update` + `response.create`.

Diseño:
- Target size configurable (default 2). Cada vez que se consume una, se
  lanza una task background para reponer.
- Las WS son one-shot: una vez asignadas a una llamada, se cierran al
  terminar — NO regresan al pool, porque la sesión Realtime es stateful
  (conversation history, tools, etc.) y mezclar conversaciones rompería
  todo.
- Si el pool está vacío al hacer `acquire`, hacemos cold start como
  fallback (no se rompe la llamada, solo no hay ahorro).
- Healthcheck: antes de devolver una WS, verificamos que sigue abierta;
  si OpenAI la cerró por idle, descartamos y abrimos otra.
- Sólo cubre el handshake. El `session.update` con el prompt sigue
  haciéndose por llamada porque el voice_prompt depende del tenant y
  pre-cachearlo por tenant complica el código sin gran ganancia hoy.
"""
import asyncio

import websockets
from websockets.asyncio.client import ClientConnection

from app.config import logger, settings


class RealtimeWSPool:
    """
    Mantiene un pool de ClientConnection pre-conectadas a OpenAI Realtime.
    Thread-safe via asyncio.Queue.
    """

    def __init__(self, target_size: int = 2):
        self.target_size = target_size
        self._available: asyncio.Queue[ClientConnection] = asyncio.Queue()
        self._closing = False
        self._refill_inflight = 0
        self._refill_lock = asyncio.Lock()

    async def start(self) -> None:
        """Llamado en lifespan startup. Lanza warm de target_size conexiones."""
        if not settings.openai_api_key:
            logger.warning("ws pool: OPENAI_API_KEY no configurada, pool deshabilitado")
            return
        for _ in range(self.target_size):
            asyncio.create_task(self._warm_one())
        logger.info("ws pool: starting target_size=%d", self.target_size)

    async def acquire(self) -> ClientConnection:
        """
        Devuelve una WS pre-conectada (o nueva en cold start si pool vacío).
        Lanza task background para reponer el pool.
        Si la WS del pool está cerrada (idle timeout de OpenAI), descarta
        y reintenta.
        """
        # Drenar conexiones cerradas hasta encontrar una sana o agotar el pool.
        while True:
            try:
                ws = self._available.get_nowait()
            except asyncio.QueueEmpty:
                logger.info("ws pool: empty, cold-start fallback")
                self._spawn_refill()
                return await self._open_fresh()

            if getattr(ws, "state", None) and ws.state.name == "OPEN":
                qsize = self._available.qsize()
                logger.info("ws pool: acquired warm conn (remaining=%d)", qsize)
                self._spawn_refill()
                return ws
            # Cerrada o degradada: descartar y seguir.
            logger.info("ws pool: discarded stale conn")
            try:
                await ws.close()
            except Exception:
                pass
            # Loop continúa para intentar siguiente o caer en QueueEmpty.

    async def shutdown(self) -> None:
        """Cierra todas las WS del pool. Llamar en lifespan shutdown."""
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
        """Si el pool quedó por debajo del target, lanzar tasks para llenar."""
        if self._closing:
            return
        deficit = self.target_size - self._available.qsize() - self._refill_inflight
        for _ in range(max(deficit, 0)):
            asyncio.create_task(self._warm_one())

    async def _warm_one(self) -> None:
        """Abre una WS y la pone en el pool (si la app aún corre)."""
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
        """Abre una WS nueva al endpoint Realtime con auth header correcto."""
        from app.realtime.events import is_v2_model
        model = settings.openai_realtime_model
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        if not is_v2_model(model):
            headers["OpenAI-Beta"] = "realtime=v1"
        return await websockets.connect(
            url,
            additional_headers=headers,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
        )


# Instancia global. Inicializada en main.lifespan.
ws_pool: RealtimeWSPool | None = None


def get_pool() -> RealtimeWSPool | None:
    return ws_pool


def init_pool(target_size: int = 2) -> RealtimeWSPool:
    global ws_pool
    ws_pool = RealtimeWSPool(target_size=target_size)
    return ws_pool
