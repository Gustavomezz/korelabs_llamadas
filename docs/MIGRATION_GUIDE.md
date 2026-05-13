# Guía de Migración — De single-tenant a multi-tenant profesional

Plan paso a paso para migrar los repos existentes a la arquitectura objetivo
descrita en [ARCHITECTURE.md](./ARCHITECTURE.md).

Orden recomendado:

1. **Pre-requisito:** aplicar el control plane a la dashboard DB
2. **Bot WhatsApp** → multi-tenant
3. **Dashboard** → UI de admin para gestionar todo
4. **Dashboard frontend** → personalización visual por cliente
5. **Llamadas** → ya está casi listo, ajustes menores

---

## 0. Pre-requisito · Aplicar control plane

En `Dashboards_Clientes_Korelabs` (donde vive la dashboard DB):

```bash
cd /Users/Compupod/Documents/Dashboards_Clientes_Korelabs
# Copia los archivos del worktree de Llamadas (este repo):
cp /Users/Compupod/Documents/Korelabs_LLamadas/infra/control_plane/schema.sql infra/control_plane/
cp /Users/Compupod/Documents/Korelabs_LLamadas/infra/control_plane/migrate.py infra/control_plane/

DASHBOARD_DATABASE_URL="postgres://..." python infra/control_plane/migrate.py --seed-existing
```

Verificar que las tablas existan:

```bash
python infra/control_plane/migrate.py --verify-only
```

Después, **sembrar las credenciales del tenant Korelabs (actual)** con un script
one-shot (o manual desde psql):

```sql
-- Asumir tenant Korelabs es id=1 (verificar con: SELECT id, slug FROM tenants)
-- Cifrar valores con Fernet usando la misma TENANT_DB_ENCRYPTION_KEY.
-- Ver scripts/seed_korelabs_credentials.py (a crear) para automatizar.

INSERT INTO tenant_credentials (tenant_id, kind, value_encrypted) VALUES
  (1, 'whatsapp_token',            '<fernet:...>'),
  (1, 'whatsapp_phone_number_id',  '<fernet:1143785418811409>'),
  (1, 'whatsapp_app_secret',       '<fernet:...>'),
  (1, 'whatsapp_verify_token',     '<fernet:korelabs_verify_2025>'),
  (1, 'openai_api_key',            '<fernet:sk-proj-...>'),
  (1, 'google_client_id',          '<fernet:670277034157-...>'),
  (1, 'google_client_secret',      '<fernet:GOCSPX-...>'),
  (1, 'chatwoot_api_token',        '<fernet:...>')
ON CONFLICT (tenant_id, kind) DO UPDATE SET value_encrypted = EXCLUDED.value_encrypted;
```

---

## 1. Bot WhatsApp → multi-tenant

Cambios estructurales en `korelabs-whatsapp-bot/`.

### 1.1 Añadir helper de lectura del control plane

Copiar `app/control_plane.py` desde este worktree de Llamadas:

```bash
cp /Users/Compupod/Documents/Korelabs_LLamadas/app/control_plane.py \
   /Users/Compupod/Documents/korelabs-whatsapp-bot/app/control_plane.py
```

Ajustar imports: `from app.config import logger, settings` → en el bot hoy
es `from app.config import logger` y las settings son módulo-level. Crear un
`settings` object o cambiar el helper para que reciba el encryption key como
parámetro.

### 1.2 Replantear `app/config.py`

Reducirlo a las pocas env vars que el bot necesita para arrancar:

```python
# Solo lo necesario para arrancar:
DASHBOARD_DATABASE_URL = os.getenv("DASHBOARD_DATABASE_URL")
TENANT_DB_ENCRYPTION_KEY = os.getenv("TENANT_DB_ENCRYPTION_KEY")

# Estas se eliminan (vienen del control plane):
# WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN,
# OPENAI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
# CHATWOOT_*, ADMIN_TOKEN

# Estas siguen como env (no son por-tenant):
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GRAPH_API_VERSION = "v21.0"
```

### 1.3 Refactor del webhook

`app/routers/webhook.py` cambia drásticamente. El payload de Meta incluye
`entry[].changes[].value.metadata.phone_number_id` — ése es el identificador
del receptor. Usarlo para resolver el tenant.

Pseudocódigo:

```python
@router.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()

    # Extraer phone_number_id (cada entrada del array puede ser un tenant distinto
    # si Meta agrupa, pero en la práctica viene 1 por POST)
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            phone_number_id = change.get("value", {}).get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue

            tenant_id = await control_plane.find_tenant_id_by_phone_number_id(
                dashboard_pool(), phone_number_id
            )
            if tenant_id is None:
                logger.warning("Webhook recibido pero phone_number_id %s no mapea a tenant", phone_number_id)
                continue

            cfg = await control_plane.get_tenant_config(dashboard_pool(), tenant_id)
            if not cfg.has_module("whatsapp_bot") or not cfg.is_active:
                logger.info("Tenant %s no tiene whatsapp_bot activo o está pausado", tenant_id)
                continue

            # Validar HMAC con el app_secret del tenant
            if cfg.feature_bool("hmac_strict", default=True):
                app_secret = await control_plane.get_credential(
                    dashboard_pool(), tenant_id, "whatsapp_app_secret", required=True,
                )
                if not _verify_hmac(request, app_secret):
                    raise HTTPException(403, "Bad signature")

            # Procesar mensaje contra la BD del tenant
            tenant_pool = await get_tenant_pool(tenant_id)
            await _process_webhook_for_tenant(tenant_id, tenant_pool, cfg, change)
```

### 1.4 Pool dinámico por tenant

Importar el patrón del repo de Llamadas:

```python
# app/database.py
async def get_tenant_pool(tenant_id: int) -> asyncpg.Pool:
    # Cache por tenant_id, con lock. Idéntico a Llamadas.
```

`init_db()` ya no crea tablas — eso lo hace **el dashboard** cuando crea el
tenant, o se centraliza en un helper `app/services/tenant_schema.py` que se
llama la primera vez que se abre un pool a la BD del tenant (igual que
Llamadas con `ensure_tenant_voice_schema`).

### 1.5 Workers por tenant

Los workers `outbox` y `meeting_requests` ya no corren contra UN DATABASE_URL.
Opciones:

**A) Worker por tenant (más complejo, mejor aislamiento):**
- Al arrancar, listar tenants con `whatsapp_bot` activo.
- Crear 1 task LISTEN por tenant.
- Al activarse un módulo (via NOTIFY al control plane), arrancar el worker.
- Al desactivarse, matarlo.

**B) Worker compartido con polling rotativo (más simple):**
- Cada N segundos, iterar tenants activos.
- Procesar pending de cada uno con `FOR UPDATE SKIP LOCKED`.
- Menos eficiente pero mucho menos código.

Recomiendo (A) para Pro. Para empezar, (B) es aceptable.

### 1.6 Adaptar `app/integrations/whatsapp.py`

Hoy hace `from app.config import WHATSAPP_TOKEN`. Necesita recibir el token
del tenant como parámetro:

```python
async def send_whatsapp_message(token: str, phone_number_id: str, to: str, message: str):
    ...
```

Y los callers (`webhook.py`, `outbox.py`) lo obtienen del control plane:

```python
creds = await control_plane.get_credentials(
    dashboard_pool(), tenant_id,
    ["whatsapp_token", "whatsapp_phone_number_id"],
)
await send_whatsapp_message(
    creds["whatsapp_token"], creds["whatsapp_phone_number_id"], to, msg,
)
```

### 1.7 Google Calendar igual

`app/integrations/google_calendar.py` también lee env vars hoy. Mismo refactor:
los `client_id`, `client_secret`, `refresh_token` vienen del control plane
por tenant.

### 1.8 Tests

Crear `tests/test_multitenant_routing.py`:
- Setup: 2 tenants en BD de test con creds distintas.
- Enviar webhook con `phone_number_id` de tenant A → mensaje guardado en BD A.
- Enviar webhook con `phone_number_id` de tenant B → mensaje guardado en BD B.
- Enviar webhook con phone_number_id inexistente → 200 ack, no se guarda nada.

---

## 2. Dashboard → UI de admin

Cambios en `Dashboards_Clientes_Korelabs/`.

### 2.1 Models nuevos

Crear `app/models/`:

- `modules.py` — CRUD de `tenant_modules`
- `credentials.py` — CRUD de `tenant_credentials` (con encrypt/decrypt)
- `features.py` — CRUD de `tenant_features`
- `branding.py` — CRUD de `tenant_branding`
- `audit.py` — escritura al `audit_log`

### 2.2 Routers admin nuevos

En `app/routers/admin.py` (o subdividir):

- `GET /admin/tenants` — lista usando `v_tenant_overview`
- `GET /admin/tenants/{id}` — vista detalle del tenant
- `POST /admin/tenants/{id}/modules` — toggle módulos
- `POST /admin/tenants/{id}/credentials` — agregar/actualizar cred (mask values)
- `POST /admin/tenants/{id}/features` — set flag
- `POST /admin/tenants/{id}/branding` — actualizar branding
- `POST /admin/tenants/{id}/pause` — pausar tenant (subscription_status='paused')
- `POST /admin/tenants/{id}/reactivate`
- `GET /admin/tenants/{id}/audit` — leer audit log del tenant

Todas las mutaciones deben llamar `write_audit_log()` automáticamente.

### 2.3 Templates Jinja

- `templates/admin/tenants_list.html`
- `templates/admin/tenant_detail.html` con tabs: Overview, Modules, Credentials, Features, Branding, Audit
- Forms con HTMX para edición in-place.

### 2.4 Migraciones del dashboard DB en arranque

Hoy `init_db()` en `app/database.py` crea solo `tenants` y `users`. Después
de aplicar el control plane SQL, esas columnas/tablas ya existirán. Pero
mantener `init_db()` simple — quitar los `ADD COLUMN IF NOT EXISTS` de
columnas que ahora viven en el schema del control plane.

---

## 3. Dashboard frontend → personalización

### 3.1 Branding al login

En `app/auth.py`, después de validar la sesión, cargar `tenant_branding` y
adjuntar al request state. Templates lo usan vía `request.state.branding`.

### 3.2 CSS vars dinámicas

En el base template del dashboard:

```html
<style>
  :root {
    --color-primary: {{ branding.primary_color or '#22c55e' }};
    --color-accent: {{ branding.accent_color or '#0ea5e9' }};
  }
</style>
{% if branding.favicon_url %}
  <link rel="icon" href="{{ branding.favicon_url }}">
{% endif %}
```

Reemplazar colores hardcoded en CSS por `var(--color-primary)`.

### 3.3 Navbar dinámica

```jinja
<nav>
  <img src="{{ branding.logo_url or '/static/logo-default.svg' }}" alt="{{ branding.business_name }}">

  {% if 'whatsapp_bot' in modules %}<a href="/inbox">Inbox</a>{% endif %}
  {% if 'voice_agent' in modules %}<a href="/calls">Llamadas</a>{% endif %}
  {% if 'google_calendar' in modules %}<a href="/citas">Citas</a>{% endif %}
  <a href="/configuracion">Configuración</a>
</nav>
```

### 3.4 Página de cliente para editar su branding

`/configuracion/branding` — el dueño del consultorio sube logo, elige color,
edita welcome_message. Validar: logos < 500KB, formato PNG/SVG, color hex válido.

---

## 4. Llamadas → ajustes menores

`Korelabs_LLamadas` ya está casi multi-tenant. Cambios mínimos:

### 4.1 Arrancar invalidation listener

En `main.py` lifespan:

```python
from app.control_plane import start_invalidation_listener, stop_invalidation_listener
from app.database import init_pools, close_pools, dashboard_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pools()
    await start_invalidation_listener(dashboard_pool())
    yield
    await stop_invalidation_listener()
    await close_pools()
```

### 4.2 Gate del servicio de llamadas por módulo activo

En `tenant_resolver.py`, después de resolver el tenant, verificar:

```python
cfg = await control_plane.get_tenant_config(dashboard_pool(), record.id)
if not cfg.has_module("voice_agent"):
    return None  # tenant no tiene voz contratada
if not cfg.is_active:
    return None  # tenant pausado
```

### 4.3 Leer Twilio creds del control plane

Si el cliente tiene su propia cuenta Twilio (caso Enterprise), leer
`twilio_account_sid` y `twilio_auth_token` del control plane. Si no, usar
las de Korelabs (env var compartida).

### 4.4 Voice prompt custom

`bot_configs.voice_prompt` ya existe en la tenant DB (ver
`app/services/tenant_schema.py`). Permitir al cliente editarlo desde el dashboard.

---

## 5. Orden de deploy

Para migrar producción sin romper a Korelabs (cliente actual):

1. **Aplicar control plane** a dashboard DB (idempotente, no rompe nada).
2. **Sembrar creds del tenant Korelabs** en `tenant_credentials`.
3. **Deploy del bot multi-tenant** EN PARALELO (dominio nuevo, ej `bot-mt.up.railway.app`).
4. **Smoke test:** mandar mensaje al WhatsApp Business desde un número de prueba,
   verificar que llega al deploy nuevo y procesa correctamente.
5. **Cambiar webhook URL en Meta** al dominio nuevo.
6. **Monitor durante 24h** — logs y métricas.
7. **Apagar deploy viejo** del bot single-tenant.
8. **Deploy del dashboard actualizado** con UI admin.
9. **Deploy del frontend personalizable.**

---

## 6. Riesgos durante la migración

| Riesgo | Mitigación |
|---|---|
| Mensajes de Korelabs caen entre el switch del webhook | Hacer switch en horario de bajo tráfico (sábado madrugada); deploys ambos vivos durante 1h |
| HMAC strict rompe el flujo si el secret está mal cifrado | Empezar con `hmac_strict=false` el primer día, activarlo después |
| Cache stale del control plane | Verificar manualmente `pg_notify` se está disparando con `psql -c "LISTEN korelabs_tenant_config_changed"` |
| Algún módulo se queda con `is_enabled=NULL` post-seed | Test SQL pre-deploy: `SELECT * FROM tenants WHERE NOT EXISTS (SELECT 1 FROM tenant_modules WHERE tenant_id = tenants.id)` |

---

## 7. Checklist final pre-cliente

Antes de cerrar el primer cliente externo, todo esto debe estar:

- [ ] Control plane aplicado y verificado en producción
- [ ] Tenant Korelabs con todas sus creds en `tenant_credentials`
- [ ] Bot multi-tenant deployado y procesando Korelabs sin issues
- [ ] HMAC strict activado y verificado
- [ ] Admin UI lista para crear nuevo tenant en < 5 min
- [ ] Branding editable por cliente
- [ ] Audit log activo en todas las mutaciones admin
- [ ] Tests E2E con 2 tenants pasando
- [ ] Privacy policy + términos publicados
- [ ] Contrato base para nuevos clientes (MX, simple)
- [ ] Playbook de onboarding (`docs/ONBOARDING.md`)
- [ ] Plan de rollback documentado por si algo falla

---

*Última actualización: 2026-05-13 · Korelabs · Migration Guide*
