"""Resuelve número Twilio entrante -> tenant + pool a su BD."""
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.database import dashboard_pool, get_tenant_pool
from app.integrations.dashboard_db import TenantRecord, fetch_tenant_by_voice_number


@dataclass(frozen=True)
class ResolvedTenant:
    record: TenantRecord
    pool: asyncpg.Pool


async def resolve_by_voice_number(e164: str) -> Optional[ResolvedTenant]:
    record = await fetch_tenant_by_voice_number(dashboard_pool(), e164)
    if record is None:
        return None
    pool = await get_tenant_pool(str(record.id), record.database_url)
    return ResolvedTenant(record=record, pool=pool)


def normalize_e164_to_wa_id(e164: str) -> str:
    """Twilio entrega numbers en E.164 ('+523321015972'). El bot de WhatsApp
    persiste sin '+'. Esta función normaliza para que el contacto sea el mismo."""
    return e164.lstrip("+")
