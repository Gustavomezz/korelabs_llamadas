"""
Aplica el schema del control plane a la BD del dashboard.

Idempotente: se puede correr múltiples veces.

Uso:
    DASHBOARD_DATABASE_URL=postgres://... python infra/control_plane/migrate.py

O con el flag --dry-run para solo imprimir el SQL sin ejecutarlo:
    python infra/control_plane/migrate.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

import asyncpg


SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"


def _needs_ssl(url: str) -> bool:
    return "rlwy.net" in url or "railway.app" in url


async def apply_schema(dashboard_database_url: str) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    ssl = "require" if _needs_ssl(dashboard_database_url) else None
    conn = await asyncpg.connect(dashboard_database_url, ssl=ssl)
    try:
        async with conn.transaction():
            await conn.execute(sql)
        print(f"✓ Schema aplicado a {_redact(dashboard_database_url)}")
    finally:
        await conn.close()


def _redact(url: str) -> str:
    """Oculta password en la URL al imprimirla."""
    try:
        prefix, rest = url.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            return f"{prefix}://{user}:***@{host}"
        return url
    except Exception:
        return "***"


async def verify_schema(dashboard_database_url: str) -> None:
    """Chequeo rápido post-migración: las tablas existen, los triggers están."""
    ssl = "require" if _needs_ssl(dashboard_database_url) else None
    conn = await asyncpg.connect(dashboard_database_url, ssl=ssl)
    try:
        expected_tables = [
            "tenants",
            "users",
            "tenant_modules",
            "tenant_credentials",
            "tenant_features",
            "tenant_branding",
            "audit_log",
        ]
        for table in expected_tables:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=$1)",
                table,
            )
            status = "✓" if exists else "✗"
            print(f"  {status} {table}")
            if not exists:
                sys.exit(f"Falta la tabla {table}; aborto.")

        # Verificar que las columnas nuevas de `tenants` están
        new_cols = ["plan", "subscription_status", "timezone", "locale"]
        for col in new_cols:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='tenants' "
                "AND column_name=$1)",
                col,
            )
            print(f"  {'✓' if exists else '✗'} tenants.{col}")
            if not exists:
                sys.exit(f"Falta la columna tenants.{col}; aborto.")

        # Verificar la función seed
        seed_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_proc WHERE proname = 'korelabs_seed_tenant_defaults')"
        )
        print(f"  {'✓' if seed_exists else '✗'} fn korelabs_seed_tenant_defaults")

        view_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.views "
            "WHERE table_schema='public' AND table_name='v_tenant_overview')"
        )
        print(f"  {'✓' if view_exists else '✗'} view v_tenant_overview")

    finally:
        await conn.close()


async def seed_existing_tenants(dashboard_database_url: str) -> None:
    """Sembrar defaults para tenants que ya existen y no tienen módulos.

    Esto cubre el caso de Korelabs (primer tenant histórico) que ya está en
    `tenants` pero no tiene fila en `tenant_modules` ni `tenant_branding`.
    Idempotente.
    """
    ssl = "require" if _needs_ssl(dashboard_database_url) else None
    conn = await asyncpg.connect(dashboard_database_url, ssl=ssl)
    try:
        rows = await conn.fetch(
            "SELECT id, slug, display_name, plan FROM tenants WHERE is_active = TRUE"
        )
        for r in rows:
            has_modules = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM tenant_modules WHERE tenant_id = $1)",
                r["id"],
            )
            if has_modules:
                print(f"  · tenant {r['slug']} ya tiene módulos, skip")
                continue
            await conn.execute(
                "SELECT korelabs_seed_tenant_defaults($1, $2)",
                r["id"],
                r["plan"] or "basic",
            )
            print(f"  ✓ seeded defaults para tenant {r['slug']} (plan={r['plan']})")
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Aplica el control plane schema")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime el SQL pero no lo ejecuta",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Solo verifica que el schema esté aplicado",
    )
    parser.add_argument(
        "--seed-existing",
        action="store_true",
        help="Después de aplicar, sembrar defaults para tenants existentes",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(SCHEMA_PATH.read_text(encoding="utf-8"))
        return

    db_url = os.getenv("DASHBOARD_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Falta DASHBOARD_DATABASE_URL (o DATABASE_URL).")

    print(f"Conectando a {_redact(db_url)}...")

    if args.verify_only:
        print("Verificando schema...")
        await verify_schema(db_url)
        return

    print("Aplicando schema...")
    await apply_schema(db_url)

    print("Verificando schema...")
    await verify_schema(db_url)

    if args.seed_existing:
        print("Sembrando defaults para tenants existentes...")
        await seed_existing_tenants(db_url)

    print("\n✓ Listo. Control plane operativo.")


if __name__ == "__main__":
    asyncio.run(main())
