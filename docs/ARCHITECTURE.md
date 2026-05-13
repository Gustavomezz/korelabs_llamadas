# Arquitectura Korelabs — Plataforma Multi-Tenant Personalizable

Documento maestro de arquitectura para la plataforma Korelabs.
Pensado para crecer de 1 builder solo a una agencia que onboardea consultorios médicos
en minutos, con personalización real por cliente.

> **Estado:** propuesta de arquitectura objetivo. La Fase 0 (control plane) está
> diseñada y lista para aplicar. Las fases siguientes están planeadas pero no
> implementadas todavía.

---

## 1. Visión

> Una sola plataforma. N clientes. Cada cliente con su propia BD, sus propios
> módulos activos, su propio branding, su propio system prompt. Onboarding en 5
> minutos sin tocar Railway. Cambios de schema versionados.

### Lo que tiene que ser cierto cuando llegue el primer cliente

1. Crear cliente nuevo = 1 form en el dashboard (no clonar proyecto Railway)
2. Cada cliente puede tener módulos distintos (WhatsApp solo, WhatsApp + Voz, etc.)
3. Cada cliente puede tener branding propio (logo, colores, nombre comercial)
4. Cambiar el system prompt del bot = editar en el dashboard, no redeploy
5. Pausar un cliente (impago) = 1 toggle, no apagar Railway
6. Audit trail de quién hizo qué cuándo

---

## 2. Mapa actual

Lo que ya existe en producción / desarrollo.

```
┌─────────────────────────────────────────────────────────────┐
│  korelabs-whatsapp-bot                                       │
│  • SINGLE-TENANT (env var DATABASE_URL)                      │
│  • Workers: outbox, meeting_requests (1 cliente)             │
│  • Webhook /webhook recibe TODOS los mensajes                │
│  • Sin migrations (CREATE TABLE IF NOT EXISTS en init)       │
└─────────────────────────────────────────────────────────────┘
              │  cada tenant = un deploy separado
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Dashboards_Clientes_Korelabs                                │
│  • MULTI-TENANT (pool dinámico por tenant)                   │
│  • Tabla `tenants` con database_url_encrypted (Fernet)       │
│  • Tabla `users` (admin / client) con tenant_id              │
│  • LISTEN/NOTIFY → SSE para tiempo real                      │
│  • Routers HTML + API + SPA (20+ archivos)                   │
└─────────────────────────────────────────────────────────────┘
              │  lee BD del tenant directo
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Korelabs_LLamadas (este repo)                               │
│  • MULTI-TENANT por número Twilio                            │
│  • tenant_resolver.py: e164 → tenant_id → pool               │
│  • Schema bootstrap idempotente por tenant                   │
└─────────────────────────────────────────────────────────────┘
```

### Lo que está bien hoy

- Dashboard ya tiene tabla `tenants` con creds encriptadas (Fernet).
- Llamadas ya lee de la dashboard DB para resolver tenant.
- Patrón pub/sub con `pg_notify` + canales `korelabs_*` es sólido.
- Cola de outbound (`outgoing_messages`) y `meeting_requests` con worker LISTEN.

### Lo que falta

- **Bot de WhatsApp es single-tenant.** El cuello de botella número uno.
- **No hay migrations.** Cambios de schema = `ALTER TABLE IF NOT EXISTS` en init.
  Con N BDs distintas, esto es frágil.
- **Configuración como env vars.** Onboarding = editar Railway.
- **Sin branding por cliente.** El dashboard es genérico.
- **Sin "módulos" formales.** No hay forma de decir "este cliente solo tiene
  WhatsApp, no Voz".
- **Sin audit log.**

---

## 3. Arquitectura objetivo

### 3.1. Componentes

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│                   ⭐ CONTROL PLANE (Dashboard DB)                │
│                                                                  │
│  Fuente única de verdad para TODA la configuración por cliente   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ tenants                                                  │   │
│  │   identidad + plan + estado + timezone                   │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │ tenant_modules                                           │   │
│  │   qué módulos tiene activos cada cliente                 │   │
│  │   (whatsapp_bot, voice_agent, calendar, chatwoot...)     │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │ tenant_credentials                                       │   │
│  │   creds encriptadas (whatsapp_token, openai_key...)      │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │ tenant_features                                          │   │
│  │   feature flags (auto_followups, vision, etc.)           │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │ tenant_branding                                          │   │
│  │   logo, colores, nombre comercial, favicon               │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │ audit_log                                                │   │
│  │   quién hizo qué cuándo (admin + cliente)                │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
        ▲                  ▲                  ▲
        │ lee creds        │ lee creds        │ lee creds
        │                  │                  │
┌───────┴──────┐    ┌──────┴────┐    ┌────────┴──────┐
│ Bot WhatsApp │    │  Voice    │    │   Dashboard   │
│              │    │           │    │               │
│ 1 deploy →   │    │ 1 deploy  │    │   1 deploy    │
│ N clientes   │    │           │    │               │
│              │    │           │    │ Branding +    │
│ Resolve por  │    │ Resolve   │    │ módulos del   │
│ phone_num_id │    │ por núm   │    │ tenant logged │
└──────────────┘    └───────────┘    └───────────────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ▼
            ┌────────────────────────────────┐
            │   TENANT DBs (1 BD por cliente)│
            │                                │
            │  conversations                 │
            │  contacts                      │
            │  meetings                      │
            │  meeting_actions               │
            │  meeting_requests              │
            │  outgoing_messages             │
            │  calls / call_transcripts      │
            │  bot_configs (system prompt)   │
            │  google_tokens                 │
            └────────────────────────────────┘
```

### 3.2. Principios

1. **Control plane = fuente de verdad.** Toda configuración (incluyendo credenciales)
   vive en la dashboard DB. Las env vars solo tienen lo que el deploy necesita para
   arrancar (sus propias creds, encryption key, URL del control plane).

2. **Tenant DB = datos operativos.** Conversaciones, citas, llamadas, contactos.
   Cada cliente tiene la suya por aislamiento real.

3. **Módulos como toggles verticales.** Cada feature mayor es un módulo. Activarlo
   o desactivarlo es un UPDATE en `tenant_modules`, sin redeploy.

4. **Branding y features = data, no código.** El frontend del dashboard lee la
   config del tenant logueado y renderiza acorde.

5. **Audit log obligatorio.** Toda mutación pasa por una función que registra.

---

## 4. Schema del control plane

Ver [infra/control_plane/schema.sql](../infra/control_plane/schema.sql) para el SQL ejecutable.
Resumen de las tablas:

### `tenants` (extendida)
```
id, slug, display_name,
database_url_encrypted,
plan (basic|pro|enterprise),
subscription_status (trial|active|paused|cancelled),
trial_ends_at, billing_email,
timezone (default 'America/Mexico_City'),
locale (default 'es-MX'),
is_active, created_at, updated_at
```

### `tenant_modules` (nueva)
```
PK (tenant_id, module_key)
module_key: whatsapp_bot | voice_agent | google_calendar |
            chatwoot | telegram_notifications | auto_followups
is_enabled (bool)
config (jsonb)  -- config específica del módulo
enabled_at, updated_at
```

### `tenant_credentials` (nueva)
```
PK (tenant_id, kind)
kind: whatsapp_token | whatsapp_phone_number_id | whatsapp_app_secret |
      openai_api_key | google_refresh_token | google_client_id |
      google_client_secret | chatwoot_api_token | twilio_account_sid |
      twilio_auth_token
value_encrypted (text, Fernet)
metadata (jsonb)  -- info no sensible (ej. email de la cuenta Google)
created_at, updated_at
```

### `tenant_features` (nueva)
```
PK (tenant_id, flag)
flag: reminder_24h | vision_enabled | auto_followups_24h |
      voice_outbound | hmac_validation_strict | ...
value (text)  -- "true" / "false" / JSON serializado
updated_at
```

### `tenant_branding` (nueva)
```
PK tenant_id
business_name, logo_url, favicon_url,
primary_color (hex), accent_color (hex),
font_family (default 'Geist'),
welcome_message (custom en el dashboard),
updated_at
```

### `audit_log` (nueva)
```
id, tenant_id (nullable), user_id (FK users),
action (created_tenant | updated_credential | toggled_module |
        paused_bot | logged_in | ...)
target_kind, target_id,
payload (jsonb)  -- before/after diff
ip_address, user_agent,
created_at
```

---

## 5. Roadmap por fases

### Fase 0 — Foundation (esta sesión) ✅

- [x] Diseñar arquitectura
- [ ] Schema SQL del control plane
- [ ] Script de migración aplicable
- [ ] Helper de lectura del control plane en Llamadas
- [ ] Documentar plan para los otros repos

**Output:** dashboard DB lista para soportar todo lo que sigue.

### Fase 1 — Bot WhatsApp multi-tenant

Cambios en `korelabs-whatsapp-bot`:

1. Agregar `DASHBOARD_DATABASE_URL` + `TENANT_DB_ENCRYPTION_KEY` como únicas env vars
   importantes (más `APP_SECRET` de Meta para HMAC).
2. Crear `app/control_plane.py` que es el helper de lectura (réplica del de Llamadas).
3. Refactor del webhook: extraer `phone_number_id` del payload de Meta, resolver tenant.
4. Resolver pool dinámico por tenant (igual que Llamadas).
5. Reemplazar `from app.config import WHATSAPP_TOKEN` por lookup en control plane.
6. Reemplazar `OPENAI_MODEL` config: leer del control plane si está, fallback a env.
7. Workers (`outbox`, `meeting_requests`): cambiar de "1 LISTEN en DB única" a
   "1 LISTEN por tenant activo + WhatsApp module habilitado".
8. Validar HMAC del webhook con `whatsapp_app_secret` del tenant.

**Output:** un solo deploy del bot atiende a todos los clientes.

### Fase 2 — Dashboard como panel de operación

Cambios en `Dashboards_Clientes_Korelabs`:

1. Página `/admin/tenants` con CRUD completo.
2. Página `/admin/tenants/{id}/modules` para activar/desactivar módulos.
3. Página `/admin/tenants/{id}/credentials` para gestionar creds (UI segura con masking).
4. Página `/admin/tenants/{id}/branding` para subir logo, elegir colores.
5. Página `/admin/tenants/{id}/features` para feature flags.
6. Hook de audit log en todas las mutaciones admin.

**Output:** Gustavo (admin) gestiona todo desde el dashboard, sin tocar BD ni Railway.

### Fase 3 — Personalización del frontend del dashboard

1. Al hacer login, el dashboard carga `tenant_branding` y `tenant_modules` del usuario.
2. Inyecta CSS vars con colores del tenant (`--primary`, `--accent`).
3. Muestra logo del tenant en navbar.
4. Renderiza tabs/módulos del menú según `tenant_modules` activos.
5. Permite al cliente editar su propio branding (subset de campos) en `/configuracion`.

**Output:** cada cliente ve un dashboard "suyo", no genérico de Korelabs.

### Fase 4 — Onboarding self-service

1. Endpoint `POST /admin/tenants/provision` que:
   - Genera nueva BD vía Railway API (o pide URL pegada manualmente como fallback)
   - Inserta tenant + módulos default
   - Inserta credentials encriptadas
   - Crea usuario "owner" del cliente
   - Devuelve link de "termina tu configuración"
2. Página pública `/setup/{token}` donde el cliente:
   - Hace OAuth de Google con su cuenta
   - Pega su WhatsApp Business token (con instrucciones)
   - Sube su logo
   - Elige color principal
3. Webhook único en Meta apunta al deploy del bot, no a uno por cliente.

**Output:** onboarding de 5 min para Gustavo + 10 min de configuración del cliente.

### Fase 5 — Operación profesional

1. **Migrations con Alembic** en los 3 backends.
   - Bot: 1 migration history.
   - Llamadas: 1 migration history.
   - Dashboard: 2 migration histories (control plane + tenant schema).
   - Script `migrate-all-tenants` que itera la tabla `tenants` y aplica.
2. **HMAC validation** del webhook de Meta con el `app_secret` por tenant.
3. **Rate limiting** en endpoints públicos (slowapi o nginx en Railway).
4. **Rotación de `ADMIN_TOKEN`** + sistema de API keys por admin.
5. **Métricas operativas** en el dashboard:
   - Conversaciones / cliente / mes
   - Citas agendadas / cliente / mes
   - Costo de OpenAI por cliente (si pasamos a key compartida)
   - Latencia p95 de respuesta del bot
6. **Billing** (manual al inicio):
   - Tabla `invoices` en control plane
   - Botón "marcar como pagado" en admin
   - Toggle `subscription_status='paused'` cuando vence

---

## 6. Modelo de personalización por cliente

Esto es la pregunta clave: **¿qué puede ser distinto entre clientes?**

| Aspecto | Dónde vive | Editable por |
|---|---|---|
| Logo del dashboard | `tenant_branding.logo_url` | Cliente (en `/configuracion`) |
| Colores | `tenant_branding.primary_color`, `accent_color` | Cliente |
| Nombre comercial | `tenant_branding.business_name` | Cliente |
| Módulos activos | `tenant_modules.is_enabled` | Admin (vendido como add-on) |
| System prompt del bot | `bot_configs.system_prompt` (tenant DB) | Cliente (con guardas) |
| Voice prompt | `bot_configs.voice_prompt` (tenant DB) | Cliente |
| Tipos de cita | `meeting_types` (tenant DB) | Cliente |
| Horarios disponibles | `meeting_types.business_hours` (jsonb) | Cliente |
| Mensajes auto (recordatorios) | `tenant_features` + tablas auxiliares | Cliente |
| Webhook de Telegram | `tenant_credentials.kind='telegram_bot_token'` | Cliente |
| Integraciones (Calendar, Chatwoot) | `tenant_modules` + `tenant_credentials` | Admin (asistido) |
| Plan / facturación | `tenants.plan`, `tenants.subscription_status` | Admin solo |
| Pause / reactivate | `tenants.is_active` | Admin solo |

**Lo que NO debe ser distinto entre clientes** (anti-personalización):
- Estructura de tablas en la tenant DB (rompería el código compartido).
- Lógica de calificación de leads.
- Política de seguridad (HMAC, rate limits).

---

## 7. Notas de implementación

### Encryption key

Hoy `TENANT_DB_ENCRYPTION_KEY` (Fernet) cifra solo las database URLs. La extendemos
a TODAS las credenciales sin cambiar la key — Fernet es simétrica y soporta múltiples
tipos de payload. Una sola key, todos los servicios la tienen como env var.

**Rotación de key:** futuro. Por ahora documentar que rotar la key requiere
re-encriptar todas las filas. Backlog.

### Caching

El control plane se lee mucho. Estrategias:

- **Pool por tenant:** ya está. Cache en memoria por proceso.
- **Module/feature lookup:** cachear en memoria con TTL 60s (igual que `bot_configs` hoy).
- **Branding:** cachear en memoria con TTL 5min. Cambios de logo no son críticos en tiempo real.
- **Invalidación:** al actualizar desde el dashboard, disparar `pg_notify('korelabs_tenant_config_updated', tenant_id)`. Los servicios escuchan y limpian su cache.

### Compatibilidad hacia atrás

El cliente actual (Korelabs mismo) ya está en producción con el bot single-tenant.
La migración debe ser sin downtime:

1. Aplicar SQL del control plane al dashboard DB (idempotente, no rompe nada).
2. Insertar al tenant existente como fila #1 en `tenants` (ya existe).
3. Crear filas en `tenant_credentials` con las creds que están en Railway env vars.
4. Crear filas en `tenant_modules` activando todos los módulos que ya tiene.
5. **Hacer deploy del bot nuevo en paralelo** con dominio distinto.
6. Cuando el deploy nuevo esté verificado, cambiar webhook de Meta al dominio nuevo.
7. Apagar deploy viejo.

---

## 8. Playbook de onboarding del primer cliente externo

Asumiendo Fase 0–4 completas:

**Gustavo (5 min):**
1. Crea nueva BD en Railway, copia DATABASE_PUBLIC_URL.
2. En el dashboard, `/admin/tenants/new`:
   - Slug, nombre, email del owner, plan
   - Pega la URL de la BD
   - Selecciona módulos del paquete contratado
3. El dashboard genera link de setup y se lo manda al cliente.

**Cliente (10 min):**
1. Abre link `/setup/{token}`
2. Hace OAuth de Google con su cuenta del consultorio (1 click)
3. Pega su token de WhatsApp Business (instrucciones visuales paso a paso)
4. Sube logo y elige color principal
5. Login. Listo.

**Tiempo total:** 15 min vs 45+ actuales. Sin tocar Railway.

---

## 9. Riesgos identificados

| Riesgo | Mitigación |
|---|---|
| Filtración de la encryption key → todas las creds expuestas | Rotación documentada + key en secret manager (Railway Secrets, no env var visible) |
| Bug en multi-tenant del bot → mensajes cruzados entre clientes | Tests E2E con 2 tenants ANTES de migrar producción; staging con 2 tenants reales |
| Crecimiento de pools de conexiones | Cerrar pools idle después de N min sin uso (LRU); monitor de conexiones por Railway |
| BD del cliente cae | Circuit breaker → marcar tenant como `paused` automáticamente; alertar |
| Cliente edita system prompt y rompe calificación | Validación + "modo seguro" con prompt base de Korelabs como fallback |

---

## 10. Lo que NO está en este plan

Por foco. Se pueden agregar después:

- Sistema de roles fino-granular (más allá de admin/client)
- White-label real (dominio propio del cliente)
- API pública para que el cliente integre con su CRM
- Reportes exportables (PDF mensual al cliente)
- Multi-región (todo en us-east hoy)
- Mobile app

---

*Última actualización: 2026-05-13 · Korelabs · Multi-tenant SaaS*
