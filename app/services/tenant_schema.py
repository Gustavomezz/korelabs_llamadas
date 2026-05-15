"""
Migraciones idempotentes.

Las corremos en cada arranque/apertura de pool. CREATE TABLE IF NOT EXISTS
y ADD COLUMN IF NOT EXISTS las hacen no-op si ya existe el schema.
"""
import asyncpg


async def ensure_dashboard_voice_columns(conn: asyncpg.Connection) -> None:
    """Agrega columnas necesarias en `tenants` (Dashboard DB) sin tocar lo existente."""
    await conn.execute(
        """
        ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS voice_phone_number_e164 VARCHAR(20),
            ADD COLUMN IF NOT EXISTS voice_enabled BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_voice_phone
            ON tenants(voice_phone_number_e164)
            WHERE voice_phone_number_e164 IS NOT NULL;
        """
    )


async def ensure_tenant_voice_schema(conn: asyncpg.Connection) -> None:
    """
    Tablas y columnas que vive en la BD del tenant (la misma del bot de WhatsApp).
    Reutilizamos `contacts.wa_id` y `meetings`/`meeting_requests` tal cual.
    """
    await conn.execute(
        """
        ALTER TABLE bot_configs
            ADD COLUMN IF NOT EXISTS voice_prompt TEXT;
        """
    )

    await conn.execute(
        """
        ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS primary_email VARCHAR(200);
        CREATE TABLE IF NOT EXISTS contact_identities (
            id SERIAL PRIMARY KEY,
            wa_id VARCHAR(20) NOT NULL,
            identity_type VARCHAR(30) NOT NULL,
            identity_value VARCHAR(255) NOT NULL,
            source VARCHAR(30) NOT NULL DEFAULT 'unknown',
            first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (wa_id, identity_type, identity_value)
        );
        CREATE INDEX IF NOT EXISTS idx_contact_identities_wa_id
            ON contact_identities(wa_id);
        CREATE INDEX IF NOT EXISTS idx_contact_identities_value
            ON contact_identities(identity_type, identity_value);
        """
    )

    await conn.execute(
        """
        ALTER TABLE meetings
            ADD COLUMN IF NOT EXISTS attendee_name VARCHAR(200),
            ADD COLUMN IF NOT EXISTS clinic_name VARCHAR(200),
            ADD COLUMN IF NOT EXISTS source_channel VARCHAR(30) NOT NULL DEFAULT 'voice',
            ADD COLUMN IF NOT EXISTS status VARCHAR(30) NOT NULL DEFAULT 'scheduled';
        CREATE INDEX IF NOT EXISTS idx_meetings_event_id ON meetings(event_id);
        CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status);
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            id BIGSERIAL PRIMARY KEY,
            call_sid TEXT UNIQUE NOT NULL,
            wa_id VARCHAR(20),
            caller_number VARCHAR(20) NOT NULL,
            to_number VARCHAR(20) NOT NULL,
            direction VARCHAR(10) NOT NULL DEFAULT 'inbound',
            status VARCHAR(20) NOT NULL DEFAULT 'ringing',
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            answered_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            duration_seconds INTEGER,
            ended_reason VARCHAR(40),
            recording_url TEXT,
            cost_usd NUMERIC(10,4),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_calls_started_at ON calls(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_calls_wa_id ON calls(wa_id);
        CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
        """
    )

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_transcripts (
            id BIGSERIAL PRIMARY KEY,
            call_id BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            tool_name VARCHAR(80),
            tool_args JSONB,
            tool_result JSONB,
            audio_offset_ms INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_call_transcripts_call_id
            ON call_transcripts(call_id, created_at);
        """
    )

    # Triggers NOTIFY: el dashboard hace LISTEN en estos canales y empuja por SSE.
    await conn.execute(
        """
        CREATE OR REPLACE FUNCTION korelabs_notify_call_event()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM pg_notify('korelabs_call_started', NEW.id::text);
            ELSIF TG_OP = 'UPDATE' THEN
                IF NEW.status = 'completed' AND OLD.status <> 'completed' THEN
                    PERFORM pg_notify('korelabs_call_ended', NEW.id::text);
                ELSE
                    PERFORM pg_notify('korelabs_call_updated', NEW.id::text);
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS korelabs_calls_notify ON calls;
        CREATE TRIGGER korelabs_calls_notify
        AFTER INSERT OR UPDATE ON calls
        FOR EACH ROW EXECUTE FUNCTION korelabs_notify_call_event();
        """
    )

    await conn.execute(
        """
        CREATE OR REPLACE FUNCTION korelabs_notify_call_transcript()
        RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('korelabs_call_transcript', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS korelabs_call_transcripts_notify ON call_transcripts;
        CREATE TRIGGER korelabs_call_transcripts_notify
        AFTER INSERT ON call_transcripts
        FOR EACH ROW EXECUTE FUNCTION korelabs_notify_call_transcript();
        """
    )
