# Runbook: aplicar multi-tenant en staging

Pasos exactos para validar end-to-end **antes de tocar producción**.

> Tiempo estimado total: ~45 min. Si todo sale bien, repetís en producción.

## Pre-requisitos

- [ ] Acceso a Railway con los proyectos: bot WhatsApp actual + dashboard
- [ ] Acceso a Meta Developers de la app Korelabs
- [ ] `TENANT_DB_ENCRYPTION_KEY` que ya usás en el dashboard (la copiamos)
- [ ] WhatsApp del cliente de prueba (puede ser tu segundo número)
- [ ] Postgres CLI (`psql`) o `pgAdmin` para inspeccionar tablas
- [ ] Python 3.11 + asyncpg + cryptography instalados localmente

```bash
# Si no los tenés:
pip install asyncpg cryptography python-dotenv
```

---

## Paso 0 · Preparar staging

Crear en Railway un nuevo "environment" o proyecto separado para no tocar
producción durante la prueba.

**Servicios necesarios en staging:**

1. **`korelabs-dashboard-staging`** — copia del dashboard
   - Postgres propio (acá vive el control plane)
   - Env vars: `DATABASE_URL`, `TENANT_DB_ENCRYPTION_KEY` (la real),
     `SESSION_SECRET`, `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD`
   - Deploy desde branch: `feat/control-plane-admin`

2. **`korelabs-tenant-db-staging`** — Postgres separado donde vivirá la
   BD del tenant Korelabs-staging (clone de las tablas del bot)
   - Copiar el `DATABASE_PUBLIC_URL`

3. **`korelabs-bot-mt-staging`** — bot multi-tenant nuevo
   - SIN su propio Postgres (lo lee del control plane)
   - Env vars:
     - `DASHBOARD_DATABASE_URL` = URL del Postgres del dashboard-staging
     - `TENANT_DB_ENCRYPTION_KEY` = la misma del dashboard
     - `OPENAI_API_KEY_FALLBACK` = tu key
     - `BOT_ADMIN_TOKEN` = generar aleatorio: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
     - `GOOGLE_REDIRECT_URI` = `https://<bot-mt-staging-url>/google/callback`
   - Deploy desde branch: `feat/multi-tenant`

---

## Paso 1 · Aplicar control plane al dashboard-staging

```bash
cd /Users/Compupod/Documents/Dashboards_Clientes_Korelabs
git checkout feat/control-plane-admin

# La URL pública del Postgres del dashboard-staging
export DASHBOARD_DATABASE_URL="postgres://postgres:xxx@viaduct.proxy.rlwy.net:zzz/railway"
export TENANT_DB_ENCRYPTION_KEY="<la misma del dashboard>"

# Aplicar schema (idempotente)
python infra/control_plane/migrate.py --seed-existing
```

**Output esperado:**
```
Conectando a postgres://postgres:***@...
Aplicando schema...
✓ Schema aplicado
Verificando schema...
  ✓ tenants
  ✓ tenant_modules
  ✓ tenant_credentials
  ✓ tenant_features
  ✓ tenant_branding
  ✓ audit_log
  ✓ tenants.plan
  ...
  ✓ fn korelabs_seed_tenant_defaults
  ✓ view v_tenant_overview
Sembrando defaults para tenants existentes...
✓ Listo. Control plane operativo.
```

Si falla algo, ver mensajes de error y resolver antes de seguir.

---

## Paso 2 · Crear el tenant Korelabs en el dashboard-staging

Si el dashboard-staging ya tiene el tenant Korelabs (de cuando arrancaste
con producción), saltarse este paso. Si es nuevo, crearlo:

**Opción A — Vía UI:**
1. Login admin en dashboard-staging
2. `/admin/tenants/new`
3. Llenar con:
   - Slug: `korelabs`
   - Display name: `Korelabs`
   - Database URL: pegá la URL del `korelabs-tenant-db-staging`
   - Owner email: tu email
   - Password: cualquier random (lo cambiás vía magic link después)

**Opción B — Vía SQL directo (si querés saltearte la UI):**
```sql
INSERT INTO tenants (slug, display_name, database_url_encrypted, plan)
VALUES (
  'korelabs', 'Korelabs',
  -- encriptar con Fernet usando la misma TENANT_DB_ENCRYPTION_KEY:
  -- python -c "from cryptography.fernet import Fernet; print(Fernet(b'<KEY>').encrypt(b'<DATABASE_URL>').decode())"
  '<output del comando de arriba>',
  'enterprise'
);
SELECT korelabs_seed_tenant_defaults(id, 'enterprise') FROM tenants WHERE slug = 'korelabs';
```

---

## Paso 3 · Sembrar las credenciales del tenant Korelabs

Estas son las env vars que hoy vive el bot single-tenant en Railway. Las
movemos al control plane.

```bash
cd /Users/Compupod/Documents/Dashboards_Clientes_Korelabs

# Tomá las env vars del bot actual desde Railway → tu servicio web → Variables
export WHATSAPP_TOKEN="EAA..."                  # tu token permanente
export WHATSAPP_PHONE_NUMBER_ID="1143785418811409"
export WHATSAPP_APP_SECRET="..."                # Meta App → Settings → Basic
export WHATSAPP_VERIFY_TOKEN="korelabs_verify_2025"
export OPENAI_API_KEY="sk-proj-..."
export GOOGLE_CLIENT_ID="670277034157-...apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="GOCSPX-..."
# Opcional:
export CHATWOOT_API_TOKEN="..."

# Dry run primero (no toca nada, solo muestra qué va a hacer)
python scripts/seed_korelabs_tenant.py --dry-run

# Si se ve bien, ejecutar
python scripts/seed_korelabs_tenant.py
```

**Output esperado:**
```
Conectando a postgres://postgres:***@...
Sembrando tenant slug=korelabs

Recolectando credenciales de env vars...
  ✓ whatsapp_token  (210 chars)
  ✓ whatsapp_phone_number_id  (16 chars)
  ✓ whatsapp_app_secret  (32 chars)
  ✓ whatsapp_verify_token  (22 chars)
  ✓ openai_api_key  (51 chars)
  ✓ google_client_id  (72 chars)
  ✓ google_client_secret  (35 chars)
  · skip chatwoot_api_token (env var no seteada)

Tenant: id=1 slug=korelabs name=Korelabs plan=enterprise

Va a UPSERT 7 credenciales:
  - whatsapp_token
  - whatsapp_phone_number_id
  ...

  ✓ tenants.whatsapp_phone_number_id = 1143785418811409
  ✓ módulos default sembrados (plan=enterprise)

✓ Listo. Credenciales en BD para tenant korelabs:
  - google_client_id  (140 bytes encriptados)
  - google_client_secret  ...
  ...
```

---

## Paso 4 · Verificar que el control plane está bien

```bash
python scripts/verify_control_plane.py
```

**Output esperado (todo ✓):**
```
[1. Schema del control plane]
  ✓ table tenants
  ✓ table tenant_modules
  ... (todas las tablas)
  ✓ fn korelabs_seed_tenant_defaults
  ✓ view v_tenant_overview

[2. Tenant 'korelabs']
  ✓ tenant id=1 name='Korelabs'
  ✓ is_active = TRUE
  ✓ subscription_status = active
  ✓ whatsapp_phone_number_id = 1143785418811409
  ✓ plan = enterprise

[3. Módulos del tenant]
  ✓ auto_followups = ON
  ✓ google_calendar = ON
  ✓ telegram_notifications = ON
  ✓ voice_agent = ON
  ✓ whatsapp_bot = ON
  ...

[4. Credenciales (descifrado test)]
  ✓ google_client_id descifra OK (plaintext: 72 chars)
  ✓ google_client_secret descifra OK ...
  ✓ openai_api_key descifra OK ...
  ✓ whatsapp_app_secret descifra OK ...
  ✓ whatsapp_phone_number_id descifra OK ...
  ✓ whatsapp_token descifra OK ...
  ✓ whatsapp_verify_token descifra OK ...
  ✓ todas las creds whatsapp_* presentes

[5. Branding]
  ✓ business_name = 'Korelabs'
  ✓ primary_color = #22c55e
  ⚠ logo_url vacío (cliente puede subirlo via magic link)

[6. Triggers pg_notify del control plane]
  ✓ trigger notify_tenant_modules_changed
  ...

============================================================
Resumen: ✓ 28   ⚠ 1   ✗ 0
============================================================

OK. Control plane listo para que el bot multi-tenant se conecte.
```

Si hay ✗, resolver antes de seguir. Errores comunes:
- `descifra: NO` → la `TENANT_DB_ENCRYPTION_KEY` que setteaste no es la
  que se usó para encriptar. Confirmá con la del dashboard.
- `whatsapp_phone_number_id NULL` → re-correr `seed_korelabs_tenant.py`.

---

## Paso 5 · Levantar el bot multi-tenant en staging

En Railway, deploy del proyecto `korelabs-bot-mt-staging` desde branch
`feat/multi-tenant`. Ya debería tener las env vars del paso 0.

Después del deploy:

```bash
# Health check público
curl https://<bot-mt-staging-url>/health
# Esperado: {"status": "ok", "active_tenants": 1}

curl https://<bot-mt-staging-url>/
# Esperado: {"status": "ok", "service": "Korelabs WhatsApp Bot", "mode": "multi-tenant"}
```

**Logs esperados en Railway al arrancar (en orden):**
```
INFO Bot config: OPENAI_MODEL=gpt-4o-mini fallback_key=set
INFO dashboard pool initialized
INFO control_plane: invalidation listener activo
INFO worker manager started
INFO worker manager LISTEN activo
INFO opened tenant pool tenant_id=1
INFO tenant schema OK
INFO started worker outbox tenant=1
INFO outbox worker LISTEN activo tenant=1
INFO started worker meeting_requests tenant=1
INFO meeting_requests worker LISTEN activo tenant=1
```

---

## Paso 6 · Cambiar webhook de Meta al bot-mt-staging

> **Atención:** Esto desconecta tu prod del bot viejo. Hacé este paso solo
> si confirmaste todo lo de arriba.

Para staging real (con un número de prueba separado), apuntás el webhook
del número de prueba al bot-mt-staging. Si NO tenés un segundo número
para staging, podés probar mandando mensajes al número actual pero el
deploy viejo va a competir.

1. Meta Developers → tu app → WhatsApp → Configuration
2. **Callback URL:** `https://<bot-mt-staging-url>/webhook`
3. **Verify token:** el mismo `whatsapp_verify_token` que sembraste
4. Click "Verify and save"
   - Meta hace GET al webhook → tu bot busca el token en
     `tenant_credentials` y devuelve 200 OK con el challenge
5. Click "Webhook fields" → subscribir a **messages**

---

## Paso 7 · Smoke test end-to-end

1. **Manda un WhatsApp** al número de prueba desde tu celular
2. Esperar respuesta del bot (5–10 seg)
3. Revisar logs en Railway del bot-mt-staging:
   ```
   INFO Webhook received: {"object":"whatsapp_business_account",...
   INFO tenant=1 from=521... (Tu Nombre): mensaje
   INFO history-aware mode tenant_id=1 ...
   INFO Tool call: ... (si Calendar)
   ```
4. **Verificá en la BD del tenant:**
   ```bash
   psql "<DATABASE_URL del tenant>" -c \
     "SELECT id, wa_id, role, LEFT(content, 60) FROM conversations ORDER BY id DESC LIMIT 5"
   ```
   Debe aparecer tu mensaje y la respuesta.
5. **Verificá en el dashboard-staging:**
   - Login como admin
   - `/admin/tenants/1/config?tab=audit` → debe haber entradas
     - `setup.link_generated` (si generaste magic link)
   - El cliente entra (vos en otro browser/incognito) a `/app/inbox`
     y debe ver el chat con tu mensaje en tiempo real (SSE).

---

## Paso 8 · Test del magic link

1. En `/admin/tenants/1` → clic "Magic link →" del usuario
2. Se abre página con la URL
3. Abrí esa URL en incógnito
4. Fijar password + (opcional) logo + colores
5. Submit → debe loguearte y mandarte a `/app/inicio`
6. Verificá que los colores aplican (si los cambiaste)

---

## Paso 9 · Test del HMAC

Para verificar que el bot rechaza webhooks falsos:

```bash
curl -X POST https://<bot-mt-staging-url>/webhook \
  -H "Content-Type: application/json" \
  -d '{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"1143785418811409"}}}]}]}'
```

**Esperado:** `{"detail":"Bad signature"}` con status 403.

Si NO falla con 403, la feature `hmac_strict` está OFF o falta el
`whatsapp_app_secret`. Para activarla:

```sql
-- En el dashboard DB
UPDATE tenant_features SET value = 'true' WHERE tenant_id = 1 AND flag = 'hmac_strict';
-- O dejarla así si querés permitir testing manual de webhooks
```

---

## Si todo OK → producción

Repetir pasos 1–7 contra tu Postgres y servicios de producción. Cambios:
- Usá `DASHBOARD_DATABASE_URL` de prod en pasos 1–4
- Deploy del bot-mt en proyecto Railway de prod
- Cambio del webhook URL en Meta = downtime de unos minutos durante la
  transición. Hacer en horario de bajo tráfico (sábado madrugada).
- Apagar deploy viejo del bot single-tenant solo después de confirmar
  24h sin issues en el nuevo.

---

## Troubleshooting

| Síntoma | Causa probable | Fix |
|---|---|---|
| `dashboard pool not initialized` | El bot no tiene `DASHBOARD_DATABASE_URL` | Setear env var y redeploy |
| `descifra: NO` | TENANT_DB_ENCRYPTION_KEY distinta | Confirmar que es la misma en bot y dashboard |
| `unknown tenant` en logs del bot | `phone_number_id` no mapea | Verificar `tenants.whatsapp_phone_number_id = '<numérico>'` |
| `Bad signature` legítimo | App Secret incorrecto en `whatsapp_app_secret` | Re-copiar desde Meta App → Settings → Basic |
| Cliente no ve sus colores | Cache 5min del SPA | Hard refresh o esperar |
| Workers no procesan outbox | Tenant sin módulo `whatsapp_bot` enabled | `/admin/tenants/{id}/config?tab=modules` activarlo |

---

*Última actualización: 2026-05-13 · Korelabs · Runbook v1.0*
