"""
Cliente del Control Plane (BD del dashboard) para Llamadas.

El control plane es la fuente única de verdad para:
  - tenants            (identidad, plan, estado)
  - tenant_modules     (qué módulos tiene activos cada cliente)
  - tenant_credentials (creds cifradas con Fernet)
  - tenant_features    (feature flags)
  - tenant_branding    (logo, colores, nombre comercial)

Este módulo expone helpers de lectura asíncrona con cache en memoria.
La cache se invalida automáticamente vía LISTEN sobre el canal
`korelabs_tenant_config_changed`.

Diseño:
  - SIN escrituras desde Llamadas. Solo el dashboard muta el control plane.
  - Cache de 60s con TTL. Invalidación inmediata vía NOTIFY cuando el
    dashboard actualiza algo (config / creds / features / branding).
  - Falla suave: si el control plane está inaccesible y hay valor cacheado,
    se usa el cacheado. Si no hay cache, se levanta excepción.

Ver: docs/ARCHITECTURE.md sección 4 para schema.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

from app.config import logger, settings

# Tiempo máximo (segundos) que un valor cacheado se considera fresco.
# Cualquier cambio en el dashboard dispara NOTIFY que invalida la cache
# antes de este TTL, así que en práctica los reads son inmediatos.
_CACHE_TTL_SECONDS = 60.0

# Canal de pub/sub que dispara el schema cuando algo cambia.
_INVALIDATION_CHANNEL = "korelabs_tenant_config_changed"


# ──────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TenantConfig:
    """Snapshot inmutable de la configuración de un tenant.

    Construido a partir de varias tablas del control plane en una sola lectura.
    Lo que el servicio necesita para atender una petición de ese tenant.
    """

    tenant_id: int
    slug: str
    display_name: Optional[str]
    plan: str
    subscription_status: str
    timezone: str
    locale: str
    is_active: bool

    # Mapa module_key -> (is_enabled, config dict).
    modules: dict[str, tuple[bool, dict[str, Any]]] = field(default_factory=dict)

    # Mapa flag -> raw value (string). Parsear con get_feature.
    features: dict[str, str] = field(default_factory=dict)

    # Branding cargado (puede estar vacío si el tenant aún no lo configuró).
    branding: dict[str, Any] = field(default_factory=dict)

    def has_module(self, key: str) -> bool:
        enabled, _ = self.modules.get(key, (False, {}))
        return enabled

    def module_config(self, key: str) -> dict[str, Any]:
        _, cfg = self.modules.get(key, (False, {}))
        return cfg

    def feature_bool(self, flag: str, default: bool = False) -> bool:
        raw = self.features.get(flag)
        if raw is None:
            return default
        return raw.strip().lower() in ("true", "1", "yes", "on")

    def feature_str(self, flag: str, default: str = "") -> str:
        return self.features.get(flag, default)


@dataclass
class _CacheEntry:
    value: TenantConfig
    fetched_at: float


# ──────────────────────────────────────────────────────────────────────────
# Fernet helpers
# ──────────────────────────────────────────────────────────────────────────


def _fernet() -> Fernet:
    if not settings.tenant_db_encryption_key:
        raise RuntimeError("TENANT_DB_ENCRYPTION_KEY not configured")
    return Fernet(settings.tenant_db_encryption_key.encode())


def decrypt_value(token: str) -> str:
    """Descifra un campo `value_encrypted` de tenant_credentials."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("invalid TENANT_DB_ENCRYPTION_KEY for stored value") from exc


def encrypt_value(plain: str) -> str:
    """Cifra un valor para guardar en tenant_credentials. Usado por scripts admin."""
    return _fernet().encrypt(plain.encode()).decode()


# ──────────────────────────────────────────────────────────────────────────
# Cache + invalidación via LISTEN
# ──────────────────────────────────────────────────────────────────────────


_config_cache: dict[int, _CacheEntry] = {}
_credentials_cache: dict[tuple[int, str], tuple[str, float]] = {}
_cache_lock = asyncio.Lock()
_invalidation_task: Optional[asyncio.Task] = None


async def start_invalidation_listener(dashboard_pool: asyncpg.Pool) -> None:
    """Arranca el background task que escucha cambios y limpia la cache.

    Llamar una sola vez en el lifespan de la app, después de init_pools().
    Idempotente: no arranca un segundo task si ya hay uno corriendo.
    """
    global _invalidation_task
    if _invalidation_task is not None and not _invalidation_task.done():
        return
    _invalidation_task = asyncio.create_task(_invalidation_loop(dashboard_pool))


async def stop_invalidation_listener() -> None:
    global _invalidation_task
    if _invalidation_task is None:
        return
    _invalidation_task.cancel()
    try:
        await _invalidation_task
    except asyncio.CancelledError:
        pass
    _invalidation_task = None


async def _invalidation_loop(dashboard_pool: asyncpg.Pool) -> None:
    """Loop infinito: mantiene una conexión LISTEN y limpia cache en NOTIFY.

    Reconecta con backoff si la conexión se cae.
    """
    backoff = 1.0
    while True:
        conn: Optional[asyncpg.Connection] = None
        try:
            conn = await dashboard_pool.acquire()

            def _callback(connection, pid, channel, payload):
                try:
                    data = json.loads(payload)
                    tenant_id = data.get("tenant_id")
                    if tenant_id is not None:
                        _invalidate_tenant_sync(int(tenant_id))
                        logger.info(
                            "control_plane: cache invalidated tenant_id=%s "
                            "kind=%s op=%s",
                            tenant_id, data.get("kind"), data.get("op"),
                        )
                except Exception as e:
                    logger.warning("control_plane: bad NOTIFY payload: %s", e)

            await conn.add_listener(_INVALIDATION_CHANNEL, _callback)
            logger.info("control_plane: invalidation listener activo")
            backoff = 1.0
            # Keep alive
            while True:
                await asyncio.sleep(60)
                if conn.is_closed():
                    raise ConnectionError("LISTEN connection closed")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("control_plane listener error: %s; reintento en %.0fs", e, backoff)
            await asyncio.sleep(min(backoff, 60.0))
            backoff = min(backoff * 2, 60.0)
        finally:
            if conn is not None:
                try:
                    await dashboard_pool.release(conn)
                except Exception:
                    pass


def _invalidate_tenant_sync(tenant_id: int) -> None:
    """Limpia cache para un tenant. Síncrono porque se invoca desde callback."""
    _config_cache.pop(tenant_id, None)
    for key in list(_credentials_cache.keys()):
        if key[0] == tenant_id:
            _credentials_cache.pop(key, None)


async def invalidate_tenant(tenant_id: int) -> None:
    """Limpia cache manualmente (test / debug)."""
    async with _cache_lock:
        _invalidate_tenant_sync(tenant_id)


# ──────────────────────────────────────────────────────────────────────────
# Lectura de configuración de tenant
# ──────────────────────────────────────────────────────────────────────────


async def get_tenant_config(
    dashboard_pool: asyncpg.Pool,
    tenant_id: int,
    *,
    force_refresh: bool = False,
) -> TenantConfig:
    """Devuelve la config completa de un tenant. Cacheada en memoria.

    Lee tenants + tenant_modules + tenant_features + tenant_branding en
    una sola transacción (consistente).
    """
    now = time.monotonic()
    if not force_refresh:
        entry = _config_cache.get(tenant_id)
        if entry and (now - entry.fetched_at) < _CACHE_TTL_SECONDS:
            return entry.value

    async with _cache_lock:
        # Double-checked locking: otro coroutine pudo haber refrescado
        entry = _config_cache.get(tenant_id)
        if entry and not force_refresh and (now - entry.fetched_at) < _CACHE_TTL_SECONDS:
            return entry.value

        config = await _fetch_tenant_config(dashboard_pool, tenant_id)
        _config_cache[tenant_id] = _CacheEntry(value=config, fetched_at=time.monotonic())
        return config


async def _fetch_tenant_config(
    dashboard_pool: asyncpg.Pool, tenant_id: int
) -> TenantConfig:
    async with dashboard_pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            tenant_row = await conn.fetchrow(
                """
                SELECT id, slug, display_name, plan, subscription_status,
                       timezone, locale, is_active
                FROM tenants
                WHERE id = $1
                """,
                tenant_id,
            )
            if tenant_row is None:
                raise LookupError(f"Tenant {tenant_id} not found")

            modules_rows = await conn.fetch(
                """
                SELECT module_key, is_enabled, config
                FROM tenant_modules
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            features_rows = await conn.fetch(
                """
                SELECT flag, value FROM tenant_features WHERE tenant_id = $1
                """,
                tenant_id,
            )
            branding_row = await conn.fetchrow(
                """
                SELECT business_name, logo_url, favicon_url,
                       primary_color, accent_color, font_family,
                       welcome_message, support_email, support_whatsapp
                FROM tenant_branding WHERE tenant_id = $1
                """,
                tenant_id,
            )

    modules: dict[str, tuple[bool, dict[str, Any]]] = {}
    for r in modules_rows:
        cfg = r["config"]
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except json.JSONDecodeError:
                cfg = {}
        modules[r["module_key"]] = (bool(r["is_enabled"]), cfg or {})

    features = {r["flag"]: r["value"] for r in features_rows}
    branding = dict(branding_row) if branding_row else {}

    return TenantConfig(
        tenant_id=tenant_row["id"],
        slug=tenant_row["slug"],
        display_name=tenant_row["display_name"],
        plan=tenant_row["plan"] or "basic",
        subscription_status=tenant_row["subscription_status"] or "active",
        timezone=tenant_row["timezone"] or "America/Mexico_City",
        locale=tenant_row["locale"] or "es-MX",
        is_active=bool(tenant_row["is_active"]),
        modules=modules,
        features=features,
        branding=branding,
    )


# ──────────────────────────────────────────────────────────────────────────
# Credenciales (descifradas on-demand, cache corta)
# ──────────────────────────────────────────────────────────────────────────


async def get_credential(
    dashboard_pool: asyncpg.Pool,
    tenant_id: int,
    kind: str,
    *,
    required: bool = True,
) -> Optional[str]:
    """Lee y descifra una credencial del tenant.

    Args:
      kind: ej 'whatsapp_token', 'openai_api_key', 'google_refresh_token'.
            Ver infra/control_plane/README.md para la lista canónica.
      required: si True y la cred no existe, lanza KeyError. Si False, None.
    """
    cached = _credentials_cache.get((tenant_id, kind))
    now = time.monotonic()
    if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    async with dashboard_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT value_encrypted FROM tenant_credentials
            WHERE tenant_id = $1 AND kind = $2
            """,
            tenant_id, kind,
        )
    if row is None:
        if required:
            raise KeyError(f"credential {kind!r} not set for tenant {tenant_id}")
        return None

    plain = decrypt_value(row["value_encrypted"])
    _credentials_cache[(tenant_id, kind)] = (plain, time.monotonic())
    return plain


async def get_credentials(
    dashboard_pool: asyncpg.Pool,
    tenant_id: int,
    kinds: list[str],
) -> dict[str, str]:
    """Lee y descifra varias credenciales de una vez.

    Lanza KeyError si alguna falta. Para opcionales, usar get_credential con
    required=False individualmente.
    """
    async with dashboard_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT kind, value_encrypted FROM tenant_credentials
            WHERE tenant_id = $1 AND kind = ANY($2::text[])
            """,
            tenant_id, kinds,
        )
    found = {r["kind"]: decrypt_value(r["value_encrypted"]) for r in rows}
    missing = [k for k in kinds if k not in found]
    if missing:
        raise KeyError(
            f"credentials missing for tenant {tenant_id}: {missing}"
        )
    now = time.monotonic()
    for k, v in found.items():
        _credentials_cache[(tenant_id, k)] = (v, now)
    return found


# ──────────────────────────────────────────────────────────────────────────
# Lookup tenant por identificadores externos
# ──────────────────────────────────────────────────────────────────────────


async def find_tenant_id_by_phone_number_id(
    dashboard_pool: asyncpg.Pool, phone_number_id: str
) -> Optional[int]:
    """Resuelve `whatsapp_phone_number_id` → tenant_id.

    Usado por el bot de WhatsApp cuando llega un webhook: el payload de Meta
    incluye el phone_number_id del receptor, que mapea a UN solo tenant.
    """
    # Lookup eficiente: usar la columna en `tenants` si está populada,
    # si no, mirar `tenant_credentials`. Preferimos `tenants` por ser un
    # índice natural (UNIQUE por implícito uso) y mucho más rápido.
    row = await dashboard_pool.fetchrow(
        """
        SELECT id FROM tenants
        WHERE whatsapp_phone_number_id = $1 AND is_active = TRUE
        LIMIT 1
        """,
        phone_number_id,
    )
    if row is not None:
        return row["id"]

    # Fallback: buscar en tenant_credentials. Esto cubre el caso donde la
    # cred ya está en su lugar pero la columna denormalizada en `tenants`
    # aún no se llenó (transición).
    async with dashboard_pool.acquire() as conn:
        creds = await conn.fetch(
            """
            SELECT tenant_id, value_encrypted
            FROM tenant_credentials
            WHERE kind = 'whatsapp_phone_number_id'
            """
        )
    for r in creds:
        try:
            if decrypt_value(r["value_encrypted"]) == phone_number_id:
                return r["tenant_id"]
        except Exception:
            continue
    return None


# ──────────────────────────────────────────────────────────────────────────
# Audit log (helper para escribir, usado por dashboard pero útil aquí también)
# ──────────────────────────────────────────────────────────────────────────


async def write_audit_log(
    dashboard_pool: asyncpg.Pool,
    *,
    action: str,
    tenant_id: Optional[int] = None,
    user_id: Optional[int] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Registra una acción en el audit log.

    Llamar desde la capa de aplicación cuando se hace cualquier mutación
    relevante. Si falla, log warning pero no rompe el flujo principal.
    """
    try:
        async with dashboard_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (tenant_id, user_id, action, target_kind,
                                       target_id, payload, ip_address, user_agent)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tenant_id,
                user_id,
                action,
                target_kind,
                target_id,
                json.dumps(payload) if payload is not None else None,
                ip_address,
                user_agent,
            )
    except Exception as e:
        logger.warning("audit_log write failed action=%s: %s", action, e)
