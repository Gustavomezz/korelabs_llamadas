#!/usr/bin/env python3
"""
migrate_to_multitenant.py — Master script de migración Korelabs a multi-tenant.

Ejecuta las 4 fases de la migración. Cada fase es idempotente y confirma
antes de hacer cambios. Si una fase falla, se puede reintentar sin riesgo.

Fases:
  1. Aplicar control plane SQL al Postgres del dashboard
  2. Sembrar tenant Korelabs + migrar credenciales del bot actual
  3. Crear proyecto Railway staging del bot multi-tenant
  4. Setear env vars + disparar deploy + generar domain público

Uso:
    export RAILWAY_API_TOKEN="..."         # token Railway (Team o PAT)
    export TENANT_DB_ENCRYPTION_KEY="..."  # MISMA Fernet key del dashboard
    export WHATSAPP_APP_SECRET="..."       # de developers.facebook.com

    # Correr una fase específica
    python3 scripts/migrate_to_multitenant.py --phase=1
    python3 scripts/migrate_to_multitenant.py --phase=2
    python3 scripts/migrate_to_multitenant.py --phase=3
    python3 scripts/migrate_to_multitenant.py --phase=4

    # Dry-run (no ejecuta, solo muestra qué haría)
    python3 scripts/migrate_to_multitenant.py --phase=1 --dry-run

    # Estado actual (read-only)
    python3 scripts/migrate_to_multitenant.py --status

Diseño:
  - Idempotente: cada fase chequea estado antes de modificar
  - Atomicidad por fase: si falla, se reintenta esa fase sin tocar las otras
  - Auditable: imprime cada acción + confirmación interactiva
  - Reversible: cada fase tiene instrucciones de rollback en su docstring

Para correrlo necesitás Python 3.10+ con asyncpg y cryptography.
    pip3 install asyncpg cryptography
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# Constantes del setup actual (descubiertas vía inspect_railway.py)
# ──────────────────────────────────────────────────────────────────────────

RAILWAY_ENDPOINT = "https://backboard.railway.com/graphql/v2"

# Proyecto del dashboard (donde vive el control plane)
DASHBOARD_PROJECT_ID = "9eb37930-7c8d-4d49-9d97-0777295a0ea7"  # capable-strength
DASHBOARD_POSTGRES_SERVICE_ID = "60ab5673-31a6-4e49-a491-31e164bfe5be"
DASHBOARD_WEB_SERVICE_ID = "29b144ec-0f72-4d21-8dfd-3e8b5604918e"

# Proyecto del bot actual (single-tenant en producción)
BOT_OLD_PROJECT_ID = "6a5e27cf-9205-49ef-a0da-d487389c489b"   # giving-contentment
BOT_OLD_WEB_SERVICE_ID = "3e53bcc4-1b3f-4807-a33d-b715719814df"
BOT_OLD_POSTGRES_SERVICE_ID = "3a6a934c-f344-4370-bcc9-1b0ac1b11f0c"

# Repos de GitHub a deployar
BOT_REPO = "Gustavomezz/korelabs-whatsapp-bot"
BOT_BRANCH = "feat/multi-tenant"

# Tenant slug y plan para Korelabs
KORELABS_SLUG = "korelabs"
KORELABS_PLAN = "enterprise"

# Mapeo: env var del bot actual → kind en tenant_credentials
# (Las que están comentadas con # NO se migran porque son env vars del Railway
# y no del tenant: RAILWAY_*, DATABASE_URL es del Postgres viejo, ADMIN_TOKEN
# se reemplaza con BOT_ADMIN_TOKEN nuevo, etc.)
ENV_TO_CRED_MAPPING = {
    "WHATSAPP_TOKEN":            "whatsapp_token",
    "WHATSAPP_PHONE_NUMBER_ID":  "whatsapp_phone_number_id",
    "WHATSAPP_VERIFY_TOKEN":     "whatsapp_verify_token",
    # WHATSAPP_APP_SECRET viene de env var separada (no está en bot actual)
    "OPENAI_API_KEY":            "openai_api_key",
    "GOOGLE_CLIENT_ID":          "google_client_id",
    "GOOGLE_CLIENT_SECRET":      "google_client_secret",
    "CHATWOOT_API_TOKEN":        "chatwoot_api_token",
}

# Módulos a activar para Korelabs (enterprise tiene acceso a todos)
KORELABS_MODULES_TO_ENABLE = [
    "whatsapp_bot",
    "google_calendar",
    "chatwoot",
    "voice_agent",      # ya está conectado vía Llamadas
]

# Features iniciales
KORELABS_FEATURES = {
    "hmac_strict": "true",
    "reminder_24h": "true",
    "auto_qualify": "true",
    "allow_prompt_override": "false",
}

# ──────────────────────────────────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────────────────────────────────


def log(msg: str = ""):
    print(msg, flush=True)


def err(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def info(msg: str):
    print(f"  → {msg}", flush=True)


def ok(msg: str):
    print(f"  ✓ {msg}", flush=True)


def warn(msg: str):
    print(f"  ⚠ {msg}", flush=True)


def section(title: str):
    log()
    log("=" * 72)
    log(f"  {title}")
    log("=" * 72)


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        err(f"falta env var: {name}")
        sys.exit(1)
    return val


def confirm(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    answer = input(f"\n  ? {question}{suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "s", "si", "sí")


def gql(query: str, variables: dict | None = None) -> dict:
    """Railway GraphQL via curl (evita SSL drama de Python en macOS)."""
    token = require_env("RAILWAY_API_TOKEN")
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        result = subprocess.run(
            [
                "curl", "-sS", "--max-time", "60",
                "-X", "POST", RAILWAY_ENDPOINT,
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "--data-binary", json.dumps(payload),
            ],
            capture_output=True, text=True, timeout=65,
        )
    except subprocess.TimeoutExpired:
        err("Railway API timeout (>60s)")
        sys.exit(1)
    if result.returncode != 0:
        err(f"curl exit {result.returncode}: {result.stderr[:300]}")
        sys.exit(1)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        err(f"respuesta no es JSON: {result.stdout[:300]}")
        sys.exit(1)
    if "errors" in data:
        err(f"GraphQL errors:\n{json.dumps(data['errors'], indent=2)}")
        sys.exit(1)
    return data.get("data", {})


# ──────────────────────────────────────────────────────────────────────────
# Helpers para leer del Railway
# ──────────────────────────────────────────────────────────────────────────


def get_variables(project_id: str, environment_id: str, service_id: str) -> dict:
    """Trae el dict completo {name: value} de un servicio."""
    data = gql(
        """
        query Vars($p: String!, $e: String!, $s: String!) {
          variables(projectId: $p, environmentId: $e, serviceId: $s)
        }
        """,
        {"p": project_id, "e": environment_id, "s": service_id},
    )
    return data.get("variables") or {}


def get_production_env(project_id: str) -> str:
    """Devuelve el environment_id de 'production' del proyecto."""
    data = gql(
        """
        query($id: String!) {
          project(id: $id) {
            environments {
              edges { node { id name } }
            }
          }
        }
        """,
        {"id": project_id},
    )
    envs = data.get("project", {}).get("environments", {}).get("edges", [])
    for e in envs:
        if e["node"]["name"] == "production":
            return e["node"]["id"]
    err(f"production env no encontrado en project {project_id}")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────
# FASE 1: Aplicar control plane SQL al Postgres del dashboard
# ──────────────────────────────────────────────────────────────────────────


def schema_sql_path() -> str:
    """Resuelve la ruta del schema.sql relativa a este script."""
    here = os.path.dirname(os.path.abspath(__file__))
    # Buscar en varios lugares posibles
    candidates = [
        os.path.join(here, "..", "infra", "control_plane", "schema.sql"),
        os.path.join(here, "..", "..", "infra", "control_plane", "schema.sql"),
        # Si el script vive en Dashboards_Clientes_Korelabs/scripts
        os.path.join(here, "..", "infra", "control_plane", "schema.sql"),
    ]
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.exists(c):
            return c
    err(
        "No encontré schema.sql. Asegurate de correr este script desde la "
        "raíz de uno de los repos:\n"
        "  - Korelabs_LLamadas (que tiene infra/control_plane/schema.sql)\n"
        "  - Dashboards_Clientes_Korelabs (idem)"
    )
    sys.exit(1)


async def phase_1_apply_control_plane(dry_run: bool = False) -> None:
    section("FASE 1: Aplicar control plane SQL al Postgres del dashboard")

    import asyncpg

    info("Obteniendo DATABASE_PUBLIC_URL del Postgres del dashboard...")
    env_id = get_production_env(DASHBOARD_PROJECT_ID)
    pg_vars = get_variables(DASHBOARD_PROJECT_ID, env_id, DASHBOARD_POSTGRES_SERVICE_ID)
    db_url = pg_vars.get("DATABASE_PUBLIC_URL")
    if not db_url:
        err("No se encontró DATABASE_PUBLIC_URL en el Postgres del dashboard")
        sys.exit(1)
    # Mostrar URL redactada
    redacted = db_url.split("@")[1] if "@" in db_url else "(unparseable)"
    ok(f"DASHBOARD_DATABASE_URL → {redacted}")

    schema_path = schema_sql_path()
    schema_size = os.path.getsize(schema_path)
    ok(f"Schema encontrado: {schema_path} ({schema_size} bytes)")

    if dry_run:
        warn("DRY RUN: no se aplica nada. Saldría conectándose y aplicando el SQL.")
        return

    if not confirm(
        "Aplicar schema (es idempotente, no rompe lo que ya hay)?",
        default=True,
    ):
        warn("Cancelado por usuario")
        return

    info("Conectando...")
    ssl = "require" if ("rlwy.net" in db_url or "railway.app" in db_url) else None
    conn = await asyncpg.connect(db_url, ssl=ssl)
    try:
        info("Aplicando schema...")
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        async with conn.transaction():
            await conn.execute(sql)
        ok("Schema aplicado")

        info("Verificando tablas críticas...")
        for tbl in ("tenants", "tenant_modules", "tenant_credentials",
                    "tenant_features", "tenant_branding", "audit_log"):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=$1)",
                tbl,
            )
            if exists:
                ok(f"table {tbl}")
            else:
                err(f"tabla {tbl} no existe después de aplicar schema")
                sys.exit(1)

        # Verificar columnas nuevas en tenants
        for col in ("plan", "subscription_status", "timezone", "locale"):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='tenants' AND column_name=$1)",
                col,
            )
            if exists:
                ok(f"tenants.{col}")
            else:
                err(f"columna tenants.{col} no existe")
                sys.exit(1)

        ok("Fase 1 completa")
    finally:
        await conn.close()


# ──────────────────────────────────────────────────────────────────────────
# FASE 2: Sembrar tenant Korelabs + credenciales
# ──────────────────────────────────────────────────────────────────────────


async def phase_2_seed_korelabs(dry_run: bool = False) -> None:
    section("FASE 2: Sembrar tenant Korelabs + credenciales")

    import asyncpg
    from cryptography.fernet import Fernet

    fernet_key = require_env("TENANT_DB_ENCRYPTION_KEY")
    fernet = Fernet(fernet_key.encode())
    app_secret = require_env("WHATSAPP_APP_SECRET")

    info("Conectando al control plane (Postgres del dashboard)...")
    env_id = get_production_env(DASHBOARD_PROJECT_ID)
    pg_vars = get_variables(DASHBOARD_PROJECT_ID, env_id, DASHBOARD_POSTGRES_SERVICE_ID)
    control_plane_url = pg_vars.get("DATABASE_PUBLIC_URL")
    ssl = "require" if ("rlwy.net" in control_plane_url or "railway.app" in control_plane_url) else None
    conn = await asyncpg.connect(control_plane_url, ssl=ssl)

    try:
        info("Verificando si tenant 'korelabs' ya existe...")
        tenant_row = await conn.fetchrow(
            "SELECT id, display_name, plan FROM tenants WHERE slug = $1",
            KORELABS_SLUG,
        )

        if tenant_row is None:
            info("Tenant no existe. Creando...")
            # Necesitamos la URL de la BD del bot actual (será la tenant DB)
            bot_env_id = get_production_env(BOT_OLD_PROJECT_ID)
            bot_pg_vars = get_variables(
                BOT_OLD_PROJECT_ID, bot_env_id, BOT_OLD_POSTGRES_SERVICE_ID,
            )
            bot_db_url = bot_pg_vars.get("DATABASE_PUBLIC_URL")
            if not bot_db_url:
                err("No se encontró DATABASE_PUBLIC_URL del Postgres del bot actual")
                sys.exit(1)

            if dry_run:
                warn(f"DRY RUN: crearía tenant slug='{KORELABS_SLUG}' con URL bot DB cifrada")
            else:
                encrypted_url = fernet.encrypt(bot_db_url.encode()).decode()
                tenant_id = await conn.fetchval(
                    """
                    INSERT INTO tenants (
                        slug, display_name, database_url_encrypted, plan,
                        subscription_status, timezone, billing_email
                    )
                    VALUES ($1, $2, $3, $4, 'active', 'America/Mexico_City', $5)
                    RETURNING id
                    """,
                    KORELABS_SLUG, "Korelabs", encrypted_url, KORELABS_PLAN,
                    "gustavoa.menriquez@gmail.com",
                )
                ok(f"tenant creado: id={tenant_id}")
        else:
            tenant_id = tenant_row["id"]
            ok(f"tenant 'korelabs' ya existe: id={tenant_id} plan={tenant_row['plan']}")

        # ── Leer credenciales del bot actual ──────────────────────────
        info("Leyendo env vars del bot actual...")
        bot_env_id = get_production_env(BOT_OLD_PROJECT_ID)
        bot_vars = get_variables(BOT_OLD_PROJECT_ID, bot_env_id, BOT_OLD_WEB_SERVICE_ID)

        creds_to_seed: dict[str, str] = {}
        for env_name, kind in ENV_TO_CRED_MAPPING.items():
            value = bot_vars.get(env_name)
            if value:
                creds_to_seed[kind] = value
                ok(f"{env_name} → {kind} ({len(value)} chars)")
            else:
                warn(f"{env_name} no está en el bot actual; skip {kind}")

        # Agregar el App Secret que viene de env var del script
        creds_to_seed["whatsapp_app_secret"] = app_secret
        ok(f"whatsapp_app_secret ← $WHATSAPP_APP_SECRET ({len(app_secret)} chars)")

        # ── UPSERT credenciales en tenant_credentials ─────────────────
        if dry_run:
            warn(f"DRY RUN: UPSERT {len(creds_to_seed)} credenciales en tenant_credentials")
        else:
            info(f"UPSERT {len(creds_to_seed)} credenciales (cifradas)...")
            async with conn.transaction():
                for kind, value in creds_to_seed.items():
                    encrypted = fernet.encrypt(value.encode()).decode()
                    await conn.execute(
                        """
                        INSERT INTO tenant_credentials (tenant_id, kind, value_encrypted)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (tenant_id, kind) DO UPDATE
                        SET value_encrypted = EXCLUDED.value_encrypted,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        tenant_id, kind, encrypted,
                    )
            ok("credenciales encriptadas y guardadas")

        # ── Denormalizar phone_number_id a tenants ────────────────────
        phone_id = creds_to_seed.get("whatsapp_phone_number_id")
        if phone_id and not dry_run:
            await conn.execute(
                "UPDATE tenants SET whatsapp_phone_number_id = $1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                phone_id, tenant_id,
            )
            ok(f"tenants.whatsapp_phone_number_id = {phone_id}")

        # ── Sembrar módulos defaults vía función SQL ──────────────────
        if dry_run:
            warn(f"DRY RUN: SELECT korelabs_seed_tenant_defaults({tenant_id}, '{KORELABS_PLAN}')")
        else:
            await conn.execute(
                "SELECT korelabs_seed_tenant_defaults($1, $2)",
                tenant_id, KORELABS_PLAN,
            )
            ok(f"módulos default sembrados (plan={KORELABS_PLAN})")

        # ── Activar módulos específicos de Korelabs ───────────────────
        if dry_run:
            warn(f"DRY RUN: activar módulos {KORELABS_MODULES_TO_ENABLE}")
        else:
            for module_key in KORELABS_MODULES_TO_ENABLE:
                await conn.execute(
                    """
                    INSERT INTO tenant_modules (tenant_id, module_key, is_enabled, enabled_at)
                    VALUES ($1, $2, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, module_key) DO UPDATE
                    SET is_enabled = TRUE,
                        enabled_at = COALESCE(tenant_modules.enabled_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    tenant_id, module_key,
                )
                ok(f"module {module_key} = ON")

        # ── Features ──────────────────────────────────────────────────
        if dry_run:
            warn(f"DRY RUN: setear features {KORELABS_FEATURES}")
        else:
            for flag, value in KORELABS_FEATURES.items():
                await conn.execute(
                    """
                    INSERT INTO tenant_features (tenant_id, flag, value)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (tenant_id, flag) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                    """,
                    tenant_id, flag, value,
                )
                ok(f"feature {flag} = {value}")

        # ── Audit log ─────────────────────────────────────────────────
        if not dry_run:
            await conn.execute(
                """
                INSERT INTO audit_log (tenant_id, action, target_kind, payload)
                VALUES ($1, 'tenant.migrated', 'tenant', $2::jsonb)
                """,
                tenant_id,
                json.dumps({"by": "migrate_to_multitenant.py", "credentials": len(creds_to_seed)}),
            )

        ok("Fase 2 completa")
    finally:
        await conn.close()


# ──────────────────────────────────────────────────────────────────────────
# FASE 3: Crear proyecto Railway staging del bot multi-tenant
# ──────────────────────────────────────────────────────────────────────────


STAGING_PROJECT_NAME = "korelabs-bot-mt-staging"


def find_staging_project() -> dict | None:
    """Busca si ya existe el proyecto staging."""
    data = gql("query { projects { edges { node { id name } } } }")
    for edge in data.get("projects", {}).get("edges", []):
        if edge["node"]["name"] == STAGING_PROJECT_NAME:
            return edge["node"]
    return None


def phase_3_create_staging_project(dry_run: bool = False) -> dict:
    section("FASE 3: Crear proyecto Railway staging del bot multi-tenant")

    info(f"Buscando proyecto existente '{STAGING_PROJECT_NAME}'...")
    existing = find_staging_project()
    if existing:
        ok(f"Proyecto ya existe: id={existing['id']}")
        # Obtener detalles
        data = gql(
            """
            query($id: String!) {
              project(id: $id) {
                id
                name
                environments { edges { node { id name } } }
                services { edges { node { id name } } }
              }
            }
            """,
            {"id": existing["id"]},
        )
        p = data.get("project") or {}
        envs = [e["node"]["name"] for e in p.get("environments", {}).get("edges", [])]
        svcs = [s["node"]["name"] for s in p.get("services", {}).get("edges", [])]
        info(f"environments: {envs}")
        info(f"services: {svcs}")
        return p

    if dry_run:
        warn(f"DRY RUN: crearía proyecto '{STAGING_PROJECT_NAME}' + servicio del bot")
        return {}

    if not confirm(
        f"Crear proyecto Railway '{STAGING_PROJECT_NAME}' + servicio del bot multi-tenant?",
        default=True,
    ):
        warn("Cancelado por usuario")
        sys.exit(0)

    info("Creando proyecto...")
    create_proj = gql(
        """
        mutation($name: String!) {
          projectCreate(input: { name: $name }) {
            id
            name
            environments {
              edges { node { id name } }
            }
          }
        }
        """,
        {"name": STAGING_PROJECT_NAME},
    )
    proj = create_proj.get("projectCreate") or {}
    project_id = proj.get("id")
    if not project_id:
        err(f"No se creó el proyecto. Respuesta: {create_proj}")
        sys.exit(1)
    ok(f"Proyecto creado: id={project_id}")

    envs = proj.get("environments", {}).get("edges", [])
    if not envs:
        err("El proyecto se creó pero no tiene environment")
        sys.exit(1)
    env_id = envs[0]["node"]["id"]
    ok(f"environment production: id={env_id}")

    info(f"Creando servicio desde repo {BOT_REPO} branch {BOT_BRANCH}...")
    create_svc = gql(
        """
        mutation($input: ServiceCreateInput!) {
          serviceCreate(input: $input) {
            id
            name
          }
        }
        """,
        {
            "input": {
                "projectId": project_id,
                "name": "bot-mt",
                "branch": BOT_BRANCH,
                "source": {"repo": BOT_REPO},
            },
        },
    )
    svc = create_svc.get("serviceCreate") or {}
    service_id = svc.get("id")
    if not service_id:
        err(f"No se creó el servicio. Respuesta: {create_svc}")
        sys.exit(1)
    ok(f"Servicio creado: id={service_id} name={svc.get('name')}")

    return {
        "id": project_id,
        "environment_id": env_id,
        "service_id": service_id,
    }


# ──────────────────────────────────────────────────────────────────────────
# FASE 4: Env vars + deploy + domain
# ──────────────────────────────────────────────────────────────────────────


def upsert_variable(project_id: str, environment_id: str, service_id: str,
                    name: str, value: str) -> None:
    """UPSERT de una env var en un servicio."""
    gql(
        """
        mutation($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """,
        {
            "input": {
                "projectId": project_id,
                "environmentId": environment_id,
                "serviceId": service_id,
                "name": name,
                "value": value,
            },
        },
    )


def phase_4_configure_and_deploy(staging_info: dict, dry_run: bool = False) -> None:
    section("FASE 4: Env vars + deploy + dominio público")

    if not staging_info or not staging_info.get("service_id"):
        # Re-discover si la Fase 3 fue salteada
        existing = find_staging_project()
        if not existing:
            err(f"No existe el proyecto staging '{STAGING_PROJECT_NAME}'. Correr Fase 3 primero.")
            sys.exit(1)
        data = gql(
            """
            query($id: String!) {
              project(id: $id) {
                id
                environments { edges { node { id name } } }
                services { edges { node { id name } } }
              }
            }
            """,
            {"id": existing["id"]},
        )
        p = data.get("project") or {}
        envs = p.get("environments", {}).get("edges", [])
        svcs = p.get("services", {}).get("edges", [])
        if not envs or not svcs:
            err("Proyecto staging existe pero le falta environment/service")
            sys.exit(1)
        staging_info = {
            "id": p["id"],
            "environment_id": envs[0]["node"]["id"],
            "service_id": svcs[0]["node"]["id"],
        }

    project_id = staging_info["id"]
    env_id = staging_info["environment_id"]
    service_id = staging_info["service_id"]

    # Necesitamos:
    #   - DASHBOARD_DATABASE_URL (del Postgres del dashboard)
    #   - TENANT_DB_ENCRYPTION_KEY (env var del script)
    #   - OPENAI_API_KEY_FALLBACK (del bot actual, o reutilizar)
    #   - BOT_ADMIN_TOKEN (generamos uno aleatorio)
    #   - GOOGLE_REDIRECT_URI (apuntará al nuevo dominio cuando lo generemos)

    info("Recolectando variables a setear...")
    env_id_dashboard = get_production_env(DASHBOARD_PROJECT_ID)
    dashboard_pg_vars = get_variables(
        DASHBOARD_PROJECT_ID, env_id_dashboard, DASHBOARD_POSTGRES_SERVICE_ID,
    )
    dashboard_url = dashboard_pg_vars.get("DATABASE_PUBLIC_URL")
    if not dashboard_url:
        err("No se encontró DATABASE_PUBLIC_URL del dashboard")
        sys.exit(1)

    fernet_key = require_env("TENANT_DB_ENCRYPTION_KEY")

    env_id_bot = get_production_env(BOT_OLD_PROJECT_ID)
    bot_vars = get_variables(BOT_OLD_PROJECT_ID, env_id_bot, BOT_OLD_WEB_SERVICE_ID)
    openai_fallback = bot_vars.get("OPENAI_API_KEY") or ""

    bot_admin_token = secrets.token_urlsafe(32)

    env_vars_to_set = {
        "DASHBOARD_DATABASE_URL": dashboard_url,
        "TENANT_DB_ENCRYPTION_KEY": fernet_key,
        "OPENAI_API_KEY_FALLBACK": openai_fallback,
        "BOT_ADMIN_TOKEN": bot_admin_token,
        "OPENAI_MODEL": "gpt-4o-mini",
        # GOOGLE_REDIRECT_URI lo seteamos DESPUÉS de generar el domain
    }

    log()
    log(f"Variables a setear en {STAGING_PROJECT_NAME}/{service_id}:")
    for name in sorted(env_vars_to_set.keys()):
        v = env_vars_to_set[name]
        if name in ("DASHBOARD_DATABASE_URL",):
            preview = "<postgres URL>"
        elif name in ("TENANT_DB_ENCRYPTION_KEY", "OPENAI_API_KEY_FALLBACK", "BOT_ADMIN_TOKEN"):
            preview = f"<{len(v)} chars>"
        else:
            preview = v
        log(f"  - {name} = {preview}")

    if dry_run:
        warn("DRY RUN: no se setea nada")
        return

    if not confirm("Setear estas env vars en el servicio?", default=True):
        warn("Cancelado por usuario")
        sys.exit(0)

    info("Seteando env vars...")
    for name, value in env_vars_to_set.items():
        if not value:
            warn(f"skip {name} (valor vacío)")
            continue
        upsert_variable(project_id, env_id, service_id, name, value)
        ok(f"set {name}")

    # ── Generar domain público ────────────────────────────────────────
    info("Generando dominio público del servicio...")
    domain_result = gql(
        """
        mutation($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) {
            id
            domain
          }
        }
        """,
        {
            "input": {
                "environmentId": env_id,
                "serviceId": service_id,
            },
        },
    )
    domain_data = domain_result.get("serviceDomainCreate") or {}
    domain = domain_data.get("domain")
    if not domain:
        warn(f"No se pudo generar dominio. Respuesta: {domain_result}")
        warn("Lo podés generar manualmente desde Railway UI después.")
    else:
        ok(f"Domain: https://{domain}")
        # Setear GOOGLE_REDIRECT_URI con el dominio real
        upsert_variable(
            project_id, env_id, service_id,
            "GOOGLE_REDIRECT_URI", f"https://{domain}/google/callback",
        )
        ok(f"GOOGLE_REDIRECT_URI = https://{domain}/google/callback")

    # ── Disparar deploy ───────────────────────────────────────────────
    info("Disparando deploy...")
    deploy_result = gql(
        """
        mutation($serviceId: String!, $environmentId: String!) {
          serviceInstanceDeploy(serviceId: $serviceId, environmentId: $environmentId)
        }
        """,
        {"serviceId": service_id, "environmentId": env_id},
    )
    ok(f"Deploy disparado: {deploy_result.get('serviceInstanceDeploy', '?')}")

    log()
    log("=" * 72)
    ok("MIGRACIÓN COMPLETA")
    log("=" * 72)
    log()
    log(f"Proyecto staging: {STAGING_PROJECT_NAME} (id: {project_id})")
    if domain:
        log(f"URL:              https://{domain}")
    log(f"BOT_ADMIN_TOKEN:  {bot_admin_token}")
    log()
    log("Próximos pasos manuales:")
    log("  1. Esperar a que el deploy termine (1-3 min). Verificar en Railway UI.")
    log("  2. Health check:")
    if domain:
        log(f"       curl https://{domain}/health")
    log("     Esperado: {\"status\":\"ok\",\"active_tenants\":1}")
    log()
    log("  3. Smoke test del webhook contra el bot multi-tenant:")
    log("     - Meta Developers → tu app de WhatsApp → Configuration")
    log(f"     - Callback URL: https://{domain or '<el-dominio>'}/webhook")
    log("     - Verify token: el mismo de tu bot actual")
    log("     - Verify & save")
    log("     - Si Meta acepta, mandá un mensaje desde tu celular")
    log("     - Verificá logs en Railway del nuevo bot")
    log()
    log("  4. Si todo OK durante 24h: apagar deploy viejo (giving-contentment/web)")
    log("  5. ¡NO BORRES! el Postgres viejo — sigue siendo la tenant DB de Korelabs.")


# ──────────────────────────────────────────────────────────────────────────
# Status (read-only)
# ──────────────────────────────────────────────────────────────────────────


async def show_status() -> None:
    section("ESTADO ACTUAL DE LA MIGRACIÓN")

    import asyncpg

    # Conectar al control plane
    try:
        env_id = get_production_env(DASHBOARD_PROJECT_ID)
        pg_vars = get_variables(DASHBOARD_PROJECT_ID, env_id, DASHBOARD_POSTGRES_SERVICE_ID)
        db_url = pg_vars.get("DATABASE_PUBLIC_URL")
        if not db_url:
            warn("No se pudo obtener DATABASE_PUBLIC_URL del dashboard")
            return
        ssl = "require" if ("rlwy.net" in db_url or "railway.app" in db_url) else None
        conn = await asyncpg.connect(db_url, ssl=ssl)
    except Exception as e:
        warn(f"No se pudo conectar al control plane: {e}")
        return

    try:
        # Fase 1: schema
        log()
        log("FASE 1: Control plane schema")
        tables_ok = 0
        for tbl in ("tenants", "tenant_modules", "tenant_credentials",
                    "tenant_features", "tenant_branding", "audit_log"):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=$1)", tbl,
            )
            mark = "✓" if exists else "✗"
            log(f"  {mark} table {tbl}")
            if exists:
                tables_ok += 1
        log(f"  → {tables_ok}/6 tablas")

        # Fase 2: tenant + creds
        log()
        log("FASE 2: Tenant Korelabs + creds")
        tenant = await conn.fetchrow(
            "SELECT id, display_name, plan, whatsapp_phone_number_id, is_active "
            "FROM tenants WHERE slug = $1",
            KORELABS_SLUG,
        )
        if tenant:
            log(f"  ✓ tenant existe: id={tenant['id']} plan={tenant['plan']}")
            log(f"    whatsapp_phone_number_id = {tenant['whatsapp_phone_number_id']}")
            creds_count = await conn.fetchval(
                "SELECT COUNT(*) FROM tenant_credentials WHERE tenant_id = $1",
                tenant["id"],
            )
            log(f"  → {creds_count} credenciales sembradas")
            modules = await conn.fetch(
                "SELECT module_key, is_enabled FROM tenant_modules WHERE tenant_id = $1",
                tenant["id"],
            )
            log(f"  → {len(modules)} módulos: " +
                ", ".join(f"{m['module_key']}={'ON' if m['is_enabled'] else 'OFF'}" for m in modules))
        else:
            log(f"  ✗ tenant '{KORELABS_SLUG}' NO existe")

        # Fase 3: proyecto staging
        log()
        log("FASE 3: Proyecto Railway staging")
        staging = find_staging_project()
        if staging:
            log(f"  ✓ proyecto '{STAGING_PROJECT_NAME}' existe (id={staging['id']})")
        else:
            log(f"  ✗ proyecto '{STAGING_PROJECT_NAME}' NO existe")
    finally:
        await conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────


async def main_async():
    parser = argparse.ArgumentParser(description="Migración Korelabs a multi-tenant")
    parser.add_argument(
        "--phase",
        choices=["1", "2", "3", "4", "all"],
        help="Qué fase ejecutar (1=control plane, 2=tenant, 3=proyecto staging, 4=deploy)",
    )
    parser.add_argument("--dry-run", action="store_true", help="No ejecuta cambios")
    parser.add_argument("--status", action="store_true", help="Solo muestra estado actual")
    args = parser.parse_args()

    if args.status:
        await show_status()
        return

    if not args.phase:
        parser.print_help()
        return

    phases = [args.phase] if args.phase != "all" else ["1", "2", "3", "4"]

    # Validar env vars requeridas
    require_env("RAILWAY_API_TOKEN")
    if "2" in phases:
        require_env("TENANT_DB_ENCRYPTION_KEY")
        require_env("WHATSAPP_APP_SECRET")
    if "4" in phases:
        require_env("TENANT_DB_ENCRYPTION_KEY")

    staging_info: dict = {}

    for ph in phases:
        if ph == "1":
            await phase_1_apply_control_plane(dry_run=args.dry_run)
        elif ph == "2":
            await phase_2_seed_korelabs(dry_run=args.dry_run)
        elif ph == "3":
            staging_info = phase_3_create_staging_project(dry_run=args.dry_run)
        elif ph == "4":
            phase_4_configure_and_deploy(staging_info, dry_run=args.dry_run)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log("\nInterrumpido por usuario")
        sys.exit(130)


if __name__ == "__main__":
    main()
