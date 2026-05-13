-- ============================================================================
-- Korelabs Control Plane — Schema
-- ============================================================================
--
-- Este SQL se aplica a la BD del DASHBOARD (la "control plane").
-- Es idempotente: se puede correr múltiples veces sin romper nada.
--
-- Crea/extiende:
--   • tenants            (extendida con plan, timezone, locale, ...)
--   • tenant_modules     (módulos activos por cliente)
--   • tenant_credentials (creds encriptadas con Fernet)
--   • tenant_features    (feature flags por cliente)
--   • tenant_branding    (logo, colores, nombre comercial)
--   • audit_log          (quién hizo qué cuándo)
--
-- Asume que las tablas `tenants` y `users` ya existen (creadas por el
-- dashboard en app/database.py).
--
-- Aplicar:
--   psql "$DASHBOARD_DATABASE_URL" -f schema.sql
--
-- O via el script Python:
--   python infra/control_plane/migrate.py
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Extender `tenants` con campos de plan / billing / locale
-- ----------------------------------------------------------------------------
-- Todas las columnas son ADD COLUMN IF NOT EXISTS para idempotencia.

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS plan VARCHAR(20) NOT NULL DEFAULT 'basic',
    ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS billing_email VARCHAR(200),
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(50) NOT NULL DEFAULT 'America/Mexico_City',
    ADD COLUMN IF NOT EXISTS locale VARCHAR(10) NOT NULL DEFAULT 'es-MX';

-- Constraints para los enum-like
DO $$ BEGIN
    ALTER TABLE tenants
        ADD CONSTRAINT tenants_plan_check
        CHECK (plan IN ('basic', 'pro', 'enterprise'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE tenants
        ADD CONSTRAINT tenants_subscription_status_check
        CHECK (subscription_status IN ('trial', 'active', 'paused', 'cancelled'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS idx_tenants_subscription_status
    ON tenants(subscription_status)
    WHERE subscription_status != 'active';


-- ----------------------------------------------------------------------------
-- 2. tenant_modules — qué módulos tiene activos cada cliente
-- ----------------------------------------------------------------------------
-- Cada cliente activa los módulos del paquete que contrató. Activar/desactivar
-- es un toggle sin redeploy. La config JSONB permite parametrizar el módulo
-- sin agregar columnas.
--
-- Módulos canónicos:
--   • whatsapp_bot           — bot de WhatsApp (config: ej. fallback_message)
--   • voice_agent            — llamadas con Realtime (config: ej. voice_name)
--   • google_calendar        — integración Calendar (config: ej. work_hours)
--   • chatwoot               — mirror a Chatwoot (config: account_id, inbox_id)
--   • telegram_notifications — alertas al dueño por Telegram
--   • auto_followups         — recordatorios y follow-ups automáticos

CREATE TABLE IF NOT EXISTS tenant_modules (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    module_key  VARCHAR(50) NOT NULL,
    is_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled_at  TIMESTAMP,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, module_key)
);

CREATE INDEX IF NOT EXISTS idx_tenant_modules_enabled
    ON tenant_modules(module_key)
    WHERE is_enabled = TRUE;


-- ----------------------------------------------------------------------------
-- 3. tenant_credentials — credenciales encriptadas por tenant
-- ----------------------------------------------------------------------------
-- Reemplaza el patrón "env var por cliente". Cada cred se cifra con Fernet
-- (misma key que database_url_encrypted en `tenants`).
--
-- `kind` canónicos:
--   • whatsapp_token            — Meta WhatsApp Business permanent token
--   • whatsapp_phone_number_id  — Phone Number ID de Meta (también vive en tenants)
--   • whatsapp_app_secret       — App Secret para verificar HMAC del webhook
--   • whatsapp_verify_token     — Verify token del webhook GET
--   • openai_api_key            — opcional; si no, se usa la key compartida
--   • google_client_id          — OAuth Google
--   • google_client_secret      — OAuth Google
--   • google_refresh_token      — token del cliente para Calendar
--   • chatwoot_api_token        — opcional
--   • twilio_account_sid        — opcional, si tiene voice_agent
--   • twilio_auth_token         — opcional
--   • telegram_bot_token        — opcional, para notificaciones
--   • telegram_chat_id          — opcional, destino de notificaciones
--
-- `metadata` JSONB para info NO sensible (email de la cuenta Google, número
-- de teléfono asociado, fecha de creación del token, etc.).

CREATE TABLE IF NOT EXISTS tenant_credentials (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind            VARCHAR(50) NOT NULL,
    value_encrypted TEXT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_tenant_credentials_tenant ON tenant_credentials(tenant_id);


-- ----------------------------------------------------------------------------
-- 4. tenant_features — feature flags por cliente
-- ----------------------------------------------------------------------------
-- Toggle de features con scope tenant. Útil para A/B testing, rollouts
-- graduales, o features pagados (vision_enabled solo en plan Pro+).
--
-- `value` es text para soportar booleanos ("true"/"false"), strings, o
-- JSON serializado para configs más complejas. La capa de Python lo parsea.
--
-- Flags canónicos:
--   • reminder_24h           — recordatorio 24h antes de la cita
--   • reminder_1h            — recordatorio 1h antes
--   • vision_enabled         — bot puede procesar imágenes (Vision)
--   • voice_outbound         — agente puede iniciar llamadas (v2)
--   • hmac_strict            — rechaza webhooks sin HMAC válido
--   • auto_qualify           — detecta y guarda calificación estructurada
--   • allow_prompt_override  — cliente puede editar su system prompt

CREATE TABLE IF NOT EXISTS tenant_features (
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    flag        VARCHAR(50) NOT NULL,
    value       TEXT NOT NULL DEFAULT 'false',
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, flag)
);


-- ----------------------------------------------------------------------------
-- 5. tenant_branding — personalización visual del dashboard
-- ----------------------------------------------------------------------------
-- Un único registro por tenant. El dashboard frontend lee estos campos al
-- login y los inyecta como CSS vars + meta tags.

CREATE TABLE IF NOT EXISTS tenant_branding (
    tenant_id        INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    business_name    VARCHAR(200),
    logo_url         TEXT,
    favicon_url      TEXT,
    primary_color    VARCHAR(7) NOT NULL DEFAULT '#22c55e',
    accent_color     VARCHAR(7) NOT NULL DEFAULT '#0ea5e9',
    font_family      VARCHAR(80) NOT NULL DEFAULT 'Geist',
    welcome_message  TEXT,
    support_email    VARCHAR(200),
    support_whatsapp VARCHAR(30),
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ----------------------------------------------------------------------------
-- 6. audit_log — auditoría de mutaciones
-- ----------------------------------------------------------------------------
-- Se llena desde la capa de aplicación (no triggers — queremos contexto
-- de user_id y request). Inmutable: nunca UPDATE/DELETE en producción.
--
-- `action` ejemplos:
--   • tenant.created · tenant.paused · tenant.reactivated · tenant.deleted
--   • credential.created · credential.updated · credential.deleted
--   • module.enabled · module.disabled · module.configured
--   • feature.set
--   • branding.updated
--   • user.created · user.password_reset · user.role_changed
--   • auth.login_success · auth.login_failed
--
-- `payload` debería tener {before, after} para mutaciones, o {input} para creates.
-- IPs y user_agent son útiles para forensics.

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      VARCHAR(80) NOT NULL,
    target_kind VARCHAR(40),
    target_id   VARCHAR(80),
    payload     JSONB,
    ip_address  VARCHAR(45),
    user_agent  TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_time
    ON audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_time
    ON audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log(action, created_at DESC);


-- ----------------------------------------------------------------------------
-- 7. Notificación cuando cambia la configuración de un tenant
-- ----------------------------------------------------------------------------
-- Los servicios (bot, llamadas) cachean config del control plane. Necesitan
-- invalidar su cache cuando algo cambia. Disparamos pg_notify desde triggers.

CREATE OR REPLACE FUNCTION korelabs_notify_tenant_config_changed()
RETURNS TRIGGER AS $$
DECLARE
    affected_tenant INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        affected_tenant := OLD.tenant_id;
    ELSE
        affected_tenant := NEW.tenant_id;
    END IF;
    PERFORM pg_notify(
        'korelabs_tenant_config_changed',
        json_build_object(
            'tenant_id', affected_tenant,
            'kind', TG_TABLE_NAME,
            'op', TG_OP
        )::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS notify_tenant_modules_changed ON tenant_modules;
CREATE TRIGGER notify_tenant_modules_changed
AFTER INSERT OR UPDATE OR DELETE ON tenant_modules
FOR EACH ROW EXECUTE FUNCTION korelabs_notify_tenant_config_changed();

DROP TRIGGER IF EXISTS notify_tenant_credentials_changed ON tenant_credentials;
CREATE TRIGGER notify_tenant_credentials_changed
AFTER INSERT OR UPDATE OR DELETE ON tenant_credentials
FOR EACH ROW EXECUTE FUNCTION korelabs_notify_tenant_config_changed();

DROP TRIGGER IF EXISTS notify_tenant_features_changed ON tenant_features;
CREATE TRIGGER notify_tenant_features_changed
AFTER INSERT OR UPDATE OR DELETE ON tenant_features
FOR EACH ROW EXECUTE FUNCTION korelabs_notify_tenant_config_changed();

DROP TRIGGER IF EXISTS notify_tenant_branding_changed ON tenant_branding;
CREATE TRIGGER notify_tenant_branding_changed
AFTER INSERT OR UPDATE OR DELETE ON tenant_branding
FOR EACH ROW EXECUTE FUNCTION korelabs_notify_tenant_config_changed();


-- ----------------------------------------------------------------------------
-- 8. Seed por defecto para nuevos tenants
-- ----------------------------------------------------------------------------
-- Función helper: cuando creas un tenant, llamarla para sembrar branding y
-- módulos del plan correspondiente. La invoca el endpoint de provisión.

CREATE OR REPLACE FUNCTION korelabs_seed_tenant_defaults(
    p_tenant_id INTEGER,
    p_plan VARCHAR DEFAULT 'basic'
) RETURNS VOID AS $$
BEGIN
    -- Branding por defecto (cliente lo personaliza después)
    INSERT INTO tenant_branding (tenant_id, business_name)
    SELECT p_tenant_id, display_name FROM tenants WHERE id = p_tenant_id
    ON CONFLICT (tenant_id) DO NOTHING;

    -- Módulos por plan
    -- basic:      whatsapp_bot
    -- pro:        whatsapp_bot + google_calendar + auto_followups
    -- enterprise: todo lo de pro + voice_agent + chatwoot + telegram_notifications
    INSERT INTO tenant_modules (tenant_id, module_key, is_enabled, enabled_at)
    VALUES (p_tenant_id, 'whatsapp_bot', TRUE, CURRENT_TIMESTAMP)
    ON CONFLICT (tenant_id, module_key) DO NOTHING;

    IF p_plan IN ('pro', 'enterprise') THEN
        INSERT INTO tenant_modules (tenant_id, module_key, is_enabled, enabled_at)
        VALUES
            (p_tenant_id, 'google_calendar', TRUE, CURRENT_TIMESTAMP),
            (p_tenant_id, 'auto_followups',  TRUE, CURRENT_TIMESTAMP)
        ON CONFLICT (tenant_id, module_key) DO NOTHING;
    END IF;

    IF p_plan = 'enterprise' THEN
        INSERT INTO tenant_modules (tenant_id, module_key, is_enabled, enabled_at)
        VALUES
            (p_tenant_id, 'voice_agent',             TRUE, CURRENT_TIMESTAMP),
            (p_tenant_id, 'chatwoot',                FALSE, NULL),
            (p_tenant_id, 'telegram_notifications',  TRUE, CURRENT_TIMESTAMP)
        ON CONFLICT (tenant_id, module_key) DO NOTHING;
    END IF;

    -- Features por defecto
    INSERT INTO tenant_features (tenant_id, flag, value)
    VALUES
        (p_tenant_id, 'reminder_24h',          'true'),
        (p_tenant_id, 'hmac_strict',           'true'),
        (p_tenant_id, 'auto_qualify',          'true'),
        (p_tenant_id, 'allow_prompt_override', 'false')
    ON CONFLICT (tenant_id, flag) DO NOTHING;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------------
-- 9. Vista de salud rápida por tenant (para admin dashboard)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_tenant_overview AS
SELECT
    t.id,
    t.slug,
    t.display_name,
    t.plan,
    t.subscription_status,
    t.trial_ends_at,
    t.timezone,
    t.is_active,
    t.created_at,
    (
        SELECT COUNT(*)
        FROM tenant_modules m
        WHERE m.tenant_id = t.id AND m.is_enabled = TRUE
    ) AS active_modules_count,
    (
        SELECT COUNT(*)
        FROM tenant_credentials c
        WHERE c.tenant_id = t.id
    ) AS credentials_count,
    EXISTS(
        SELECT 1 FROM tenant_branding b
        WHERE b.tenant_id = t.id AND b.logo_url IS NOT NULL
    ) AS has_branding
FROM tenants t;


-- ============================================================================
-- Fin del schema. Para revertir todo:
--
--   DROP VIEW IF EXISTS v_tenant_overview;
--   DROP FUNCTION IF EXISTS korelabs_seed_tenant_defaults(INTEGER, VARCHAR);
--   DROP TABLE IF EXISTS audit_log;
--   DROP TABLE IF EXISTS tenant_branding;
--   DROP TABLE IF EXISTS tenant_features;
--   DROP TABLE IF EXISTS tenant_credentials;
--   DROP TABLE IF EXISTS tenant_modules;
--   DROP FUNCTION IF EXISTS korelabs_notify_tenant_config_changed();
--   -- (no revertimos las columnas agregadas a tenants — son aditivas)
-- ============================================================================
