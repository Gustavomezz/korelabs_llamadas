"""Conservative contact identity updates shared by voice flows."""
from __future__ import annotations

import re

import asyncpg


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str | None) -> str:
    value = (email or "").strip().lower()
    return value if _EMAIL_RE.match(value) else ""


def clean_name(name: str | None) -> str:
    value = " ".join((name or "").strip().split())
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8 and len(digits) >= len(value.replace(" ", "")) - 2:
        return ""
    if value.lower().startswith(("whatsapp ", "cliente ", "prospecto ")):
        return ""
    return value[:200]


async def upsert_contact_identity(
    pool: asyncpg.Pool,
    *,
    wa_id: str,
    name: str | None = None,
    email: str | None = None,
    source: str = "unknown",
) -> None:
    wa_id = (wa_id or "").strip().lstrip("+")
    if not wa_id:
        return

    display_name = clean_name(name)
    primary_email = normalize_email(email)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO contacts (wa_id, name, primary_email)
                VALUES ($1, NULLIF($2, ''), NULLIF($3, ''))
                ON CONFLICT (wa_id) DO UPDATE
                SET name = CASE
                        WHEN NULLIF($2, '') IS NULL THEN contacts.name
                        WHEN contacts.name IS NULL OR contacts.name = '' THEN EXCLUDED.name
                        WHEN contacts.name ~ '^\\+?[0-9 ]+$' THEN EXCLUDED.name
                        WHEN contacts.name ILIKE 'WhatsApp %' THEN EXCLUDED.name
                        ELSE contacts.name
                    END,
                    primary_email = COALESCE(NULLIF(contacts.primary_email, ''), EXCLUDED.primary_email)
                """,
                wa_id,
                display_name,
                primary_email,
            )

            if primary_email:
                await conn.execute(
                    """
                    INSERT INTO contact_identities (wa_id, identity_type, identity_value, source)
                    VALUES ($1, 'email', $2, $3)
                    ON CONFLICT (wa_id, identity_type, identity_value) DO UPDATE
                    SET last_seen = CURRENT_TIMESTAMP,
                        source = EXCLUDED.source
                    """,
                    wa_id,
                    primary_email,
                    source,
                )
