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
    """
    Convierte un número en E.164 ('+523131088881') al formato de wa_id que
    usa WhatsApp Cloud API (idéntico al que persiste el bot de WhatsApp).

    Casos:
    - General: quita el '+'.
    - México móvil: WhatsApp inserta un '1' entre el código de país '52' y
      los 10 dígitos del celular. Twilio NO lo hace. Sin esta corrección,
      el mismo lead aparece como dos contactos distintos
      ('5213131088881' creado por el bot WA vs '523131088881' creado por
      este servicio). El '1' móvil aplica solo a celulares MX (los fijos
      MX vienen en formato '52' + 10 dígitos sin el '1', y la propia
      WhatsApp no agrega nada para fijos).

    Heurística MX móvil: si después de quitar el '+' el número empieza con
    '52' y tiene exactamente 12 dígitos (52 + 10 dígitos celular), es
    móvil → insertamos el '1'. Si tiene 13 dígitos ya, asumimos que viene
    pre-normalizado y no tocamos.
    """
    s = (e164 or "").lstrip("+")
    if s.startswith("52") and len(s) == 12 and s.isdigit():
        return "521" + s[2:]
    return s
