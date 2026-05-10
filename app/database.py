"""
Pools de Postgres.

Dashboard DB:
  Pool central que resuelve `tenants` (slug, voice_phone_number_e164,
  database_url_encrypted con Fernet).

Tenant DBs:
  Una por cliente. Cacheadas por tenant_id. Es la MISMA BD que usa el bot
  de WhatsApp del cliente — compartimos contacts/meetings/bot_configs y
  agregamos calls/call_transcripts.
"""
import asyncio
from typing import Optional

import asyncpg

from app.config import logger, settings
from app.services.tenant_schema import ensure_dashboard_voice_columns, ensure_tenant_voice_schema

_dashboard_pool: Optional[asyncpg.Pool] = None
_tenant_pools: dict[str, asyncpg.Pool] = {}
_tenant_pool_lock = asyncio.Lock()


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
    async with _dashboard_pool.acquire() as conn:
        await ensure_dashboard_voice_columns(conn)
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


async def get_tenant_pool(tenant_id: str, database_url: str) -> asyncpg.Pool:
    """
    Devuelve un pool a la BD del tenant. La primera vez que se pide un tenant
    abre el pool, corre las migraciones idempotentes (calls + call_transcripts
    + voice_prompt en bot_configs) y cachea el pool.
    """
    pool = _tenant_pools.get(tenant_id)
    if pool is not None:
        return pool
    async with _tenant_pool_lock:
        pool = _tenant_pools.get(tenant_id)
        if pool is not None:
            return pool
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        async with pool.acquire() as conn:
            await ensure_tenant_voice_schema(conn)
        _tenant_pools[tenant_id] = pool
        logger.info("opened tenant pool tenant_id=%s", tenant_id)
        return pool
