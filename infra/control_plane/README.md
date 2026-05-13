# Control Plane — Infra

El **control plane** es la BD del dashboard, extendida con tablas para configuración,
credenciales, módulos, branding y audit log por cliente.

Es la fuente única de verdad de TODO lo que hace personalizable a cada cliente.

## Archivos

- [`schema.sql`](./schema.sql) — DDL idempotente del control plane.
- [`migrate.py`](./migrate.py) — script que aplica el schema y verifica.

## Cómo aplicar

### Primera vez

```bash
export DASHBOARD_DATABASE_URL="postgres://..."
python infra/control_plane/migrate.py --seed-existing
```

`--seed-existing` siembra módulos/branding/features default para los tenants que
ya existen en producción (Korelabs mismo).

### Verificar que está aplicado

```bash
python infra/control_plane/migrate.py --verify-only
```

### Ver el SQL sin ejecutar

```bash
python infra/control_plane/migrate.py --dry-run
```

## Cómo se usa desde los servicios

Los servicios (bot, llamadas, dashboard) abren un pool a la BD del dashboard
y leen de estas tablas. Ver:

- `Korelabs_LLamadas/app/integrations/dashboard_db.py` — patrón de lectura
- (próximo) `app/control_plane.py` con helpers para credentials/modules/features

## Convenciones

### Credenciales (`tenant_credentials.kind`)

| Kind | Cifrado | Usado por |
|---|---|---|
| `whatsapp_token` | sí (Fernet) | bot |
| `whatsapp_phone_number_id` | sí | bot (mapping) |
| `whatsapp_app_secret` | sí | bot (HMAC) |
| `whatsapp_verify_token` | sí | bot |
| `openai_api_key` | sí | bot, llamadas (opcional) |
| `google_client_id` | sí | bot (Calendar) |
| `google_client_secret` | sí | bot |
| `google_refresh_token` | sí | bot (cuenta del cliente) |
| `chatwoot_api_token` | sí | bot |
| `twilio_account_sid` | sí | llamadas |
| `twilio_auth_token` | sí | llamadas |
| `telegram_bot_token` | sí | bot (notificaciones a Gustavo o al cliente) |
| `telegram_chat_id` | sí | bot |

### Módulos (`tenant_modules.module_key`)

| Module | Plan mínimo | Notas |
|---|---|---|
| `whatsapp_bot` | basic | el core |
| `google_calendar` | pro | agendamiento automático |
| `auto_followups` | pro | recordatorios |
| `chatwoot` | enterprise | mirror para soporte humano |
| `telegram_notifications` | enterprise | alertas al owner |
| `voice_agent` | enterprise | llamadas con Realtime |

### Features (`tenant_features.flag`)

| Flag | Default | Notas |
|---|---|---|
| `reminder_24h` | true | recordatorio el día anterior |
| `reminder_1h` | false | recordatorio 1h antes |
| `vision_enabled` | false | bot puede procesar imágenes |
| `voice_outbound` | false | agente puede iniciar llamadas (v2) |
| `hmac_strict` | true | rechaza webhooks sin HMAC válido |
| `auto_qualify` | true | extrae y guarda calificación estructurada |
| `allow_prompt_override` | false | cliente puede editar su system prompt |

## Notificaciones (pub/sub)

Cualquier cambio en `tenant_modules`, `tenant_credentials`, `tenant_features` o
`tenant_branding` dispara:

```
pg_notify('korelabs_tenant_config_changed', '{"tenant_id":N, "kind":"<tabla>", "op":"INSERT|UPDATE|DELETE"}')
```

Los servicios escuchan este canal e invalidan su cache cuando llega un cambio
de su tenant.

## Audit log

Toda mutación admin debe pasar por un helper que registre en `audit_log`. No
hay triggers para esto a propósito — necesitamos `user_id` y contexto de
request que solo la capa de aplicación conoce.

Acciones canónicas (lista no exhaustiva):

- `tenant.created` · `tenant.paused` · `tenant.reactivated`
- `credential.created` · `credential.updated` · `credential.deleted`
- `module.enabled` · `module.disabled` · `module.configured`
- `feature.set`
- `branding.updated`
- `user.created` · `user.password_reset`
- `auth.login_success` · `auth.login_failed`

Payload sugerido: `{"before": {...}, "after": {...}}` para mutaciones,
`{"input": {...}}` para creates.

## Próximos pasos

1. Aplicar este schema a la BD del dashboard en staging.
2. Implementar `app/control_plane.py` en cada servicio (helpers de lectura).
3. Migrar el bot de WhatsApp a leer credenciales del control plane.
4. Construir UI admin en el dashboard (`/admin/tenants/{id}/...`).

Ver [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) para el plan completo.
