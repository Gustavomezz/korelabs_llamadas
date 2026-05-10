"""
Pools de Postgres.

- _dashboard_pool: conexión a la Dashboard DB central (tenants + users).
  Se usa para resolver el tenant a partir del número Twilio entrante y
  desencriptar el database_url del tenant.
- _tenant_pools: pools cacheados por tenant_id, conectados a la BD del bot
  del cliente. Ahí viven contacts, conversations, calls, call_transcripts.
"""
from typing import Optional

import asyncpg

from app.config import logger, settings

_dashboard_pool: Optional[asyncpg.Pool] = None
_tenant_pools: dict[str, asyncpg.Pool] = {}


async def init_pools() -> None:
    global _dashboard_pool
    if not settings.dashboard_database_url:
        logger.warning("DASHBOARD_DATABASE_URL not set; dashboard pool disabled")
        return
    _dashboard_pool = await asyncpg.create_pool(
        settings.dashboard_database_url,
        min_size=1,
        max_size=5,
        command_timeout=10,
    )
    logger.info("dashboard pool initialized")


async def close_pools() -> None:
    global _dashboard_pool
    if _dashboard_pool is not None:
        await _dashboard_pool.close()
        _dashboard_pool = None
    for tenant_id, pool in list(_tenant_pools.items()):
        await pool.close()
        del _tenant_pools[tenant_id]


def dashboard_pool() -> asyncpg.Pool:
    if _dashboard_pool is None:
        raise RuntimeError("dashboard pool not initialized")
    return _dashboard_pool
