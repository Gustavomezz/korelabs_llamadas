# Playbook de Onboarding — Korelabs

Cómo onboardear un cliente nuevo de manera profesional y consistente.
Este documento es la fuente de verdad. Si algo del proceso cambia, se
actualiza aquí.

> **Estado:** versión 1.1 (manual asistido con magic link).
>
> **Restricción importante:** Korelabs aún no tiene verificación Meta como
> Tech Provider, por lo que NO existe self-service para vincular cuentas
> WhatsApp Business. Las credenciales WhatsApp las administra Gustavo
> manualmente. El cliente solo fija su password y personaliza su branding
> vía magic link.

---

## Roles

- **Comercial (Gustavo):** descubrimiento, propuesta, cierre, onboarding inicial.
- **Implementación (Gustavo + Claude Code):** provisión técnica.
- **Cliente:** acción mínima requerida (oauth Google + token WhatsApp + logo).

---

## Fase 1 — Pre-venta (antes de firmar)

**Objetivo:** que el cliente entienda qué va a tener y firme con expectativas claras.

### Checklist

- [ ] Demo personalizada con su nombre de consultorio (no demo genérica)
- [ ] Enviar propuesta por escrito con:
  - Módulos incluidos (basic / pro / enterprise)
  - Pricing en MXN/mes
  - Qué necesita el cliente proveer (cuenta Meta Business, cuenta Google, etc.)
  - Tiempos: "5–7 días para estar live"
- [ ] Pedir información mínima del consultorio:
  - Nombre comercial
  - Especialidad (dentista, derma, etc.)
  - Tipo de citas que ofrece (primera vez, control, urgencia, etc.)
  - Horario de atención
  - Persona responsable (nombre + email + WhatsApp)
- [ ] Firmar contrato base + DPA (template en `docs/legal/`)

### Plan recomendado por tipo de cliente

| Cliente | Plan | Justificación |
|---|---|---|
| Consultorio dental 1 doctor, < 100 pacientes/mes | basic | WhatsApp solo cubre el dolor inicial |
| Consultorio multi-doctor o > 200 pac/mes | pro | Necesita calendario integrado y follow-ups automáticos |
| Clínica con recepción dedicada | enterprise | Necesita Chatwoot (humano + bot) y reportería |
| Cliente que quiere voz | enterprise | Voice agent solo en este plan |

---

## Fase 2 — Provisión técnica

**Objetivo:** crear el tenant en la plataforma. ≤ 5 min de Gustavo.

### Requisitos previos (que el cliente debe tener)

- [ ] Cuenta Meta for Business activa con un número de WhatsApp aprobado
- [ ] Cuenta Google para Calendar (idealmente del consultorio, no personal)
- [ ] (Opcional) Cuenta Twilio si quiere Voice y prefiere su propia cuenta

### Pasos (flujo actual — manual con magic link)

**Tiempo total:** ~20–25 min Gustavo + ~5 min cliente.

1. **Crear BD del cliente en Railway**
   - Ir al proyecto template "korelabs-tenant-db"
   - Click "Duplicate" → renombrar a `korelabs-tenant-{slug}`
   - Copiar `DATABASE_PUBLIC_URL`

2. **Crear tenant en el dashboard**
   - Login como admin en `https://dashboard.korelabs.app`
   - `/admin/tenants/new` → llenar:
     - Slug: kebab-case (`dental-roma`)
     - Display name: nombre comercial del consultorio
     - Database URL: pegar la URL copiada
     - Owner email: del responsable del consultorio
     - Password inicial: cualquier valor random (el cliente la sobrescribe vía magic link)
   - Submit → el sistema crea el tenant + el user owner.

3. **Cambiar plan del tenant (si aplica)**
   - Por ahora se hace vía SQL hasta que tengamos UI:
     ```sql
     UPDATE tenants SET plan = 'pro' WHERE slug = 'dental-roma';
     ```
   - Re-correr `SELECT korelabs_seed_tenant_defaults(id, 'pro')` para sembrar
     módulos del plan correcto.

4. **Pedir las credenciales al cliente**
   - El cliente debe enviarte (por canal seguro: WhatsApp, no email):
     - `WHATSAPP_TOKEN` (System User permanent token desde Meta Developers)
     - `WHATSAPP_PHONE_NUMBER_ID`
     - `WHATSAPP_APP_SECRET` (Settings → Basic en Meta App)
     - `WHATSAPP_VERIFY_TOKEN` (cualquier string que el cliente elija;
       lo configurará después en Meta Developers)
   - Ver Apéndice A más abajo para mandarle al cliente las instrucciones.

5. **Cargar credenciales en el control plane**
   - `/admin/tenants/{id}/config?tab=credentials`
   - Pegar cada cred en su fila. Se cifran con Fernet antes de guardar.
   - Si plan ≥ pro: agregar `google_client_id` y `google_client_secret`
     (los compartidos del proyecto Korelabs en GCP, NO del cliente).

6. **Cliente configura webhook en Meta**
   - El cliente va a developers.facebook.com → su app → WhatsApp → Configuration
   - Callback URL: `https://bot.korelabs.app/webhook` (la URL única del bot
     multi-tenant)
   - Verify token: el mismo string que cargaste en el paso 5
   - Suscribir a: `messages`

7. **Smoke test**
   - Mandar WhatsApp al número del cliente desde otro celular
   - Verificar en logs de Railway que el bot resuelve al tenant correcto
   - Verificar que aparece en `/conversations/{tenant-slug}/{wa_id}` del bot

8. **Generar magic link de setup para el cliente**
   - En `/admin/tenants/{id}` → al lado del user → botón "Magic link →"
   - Se abre página con la URL copiable (válida 7 días)
   - Copiar el link

9. **Mandar magic link al cliente**
   - Plantilla (WhatsApp):
     ```
     Hola {nombre}, tu plataforma Korelabs ya está activa. 🎉

     Para fijar tu contraseña y personalizar tu dashboard, abre este link:
     {magic_link}

     Te tomará 2 minutos. Una vez que termines, quedarás logueado
     automáticamente.

     Cualquier duda, aquí estoy.
     ```

10. **Cliente abre el link**
    - Fija su password
    - (Opcional) Sube logo, elige color principal, escribe welcome message
    - Click "Terminar y entrar al dashboard" → queda logueado en `/app/inicio`

### Pasos (v2 — self-service, futuro)

Requiere obtener **verificación de Meta como Tech Provider** (ver
[memory:korelabs_meta_verification](../../.claude/projects/-Users-Compupod-Documents-Korelabs-LLamadas/memory/korelabs_meta_verification.md)).
Cuando esté disponible:

1. Gustavo crea tenant en 1 form.
2. Sistema genera magic link.
3. Cliente abre el link y completa:
   - **WhatsApp Embedded Signup** (1 click, sin tokens manuales)
   - Logo + colores
   - Password
4. Total: 10 min cliente, 5 min Gustavo.

Hasta entonces, mantener el flujo manual de arriba.

---

## Fase 3 — Personalización inicial

**Objetivo:** dejar el dashboard del cliente con su identidad antes de que entre.

### Branding

En `/admin/tenants/{id}/branding`:

- [ ] **Business name:** nombre comercial (ej. "Clínica Dental Roma")
- [ ] **Logo:** subir el del cliente (formato SVG o PNG transparente, max 500KB)
- [ ] **Primary color:** color principal de su marca (hex)
- [ ] **Accent color:** complementario
- [ ] **Welcome message:** "Hola Dr. {nombre}, bienvenido al panel de Korelabs"

### System prompt del bot

Adaptar el prompt default al nicho específico. Va en `bot_configs.system_prompt`
de la **tenant DB** (no del control plane).

Plantilla base (consultorio dental, ajustar para otros nichos):

```
Eres Kora, asistente virtual de {business_name}, un consultorio dental
ubicado en {ciudad}.

Tu misión es:
1. Saludar al paciente con calidez profesional
2. Entender qué necesita (cita primera vez, control, urgencia, presupuesto)
3. Si es urgencia: derivar al doctor inmediatamente
4. Si es cita: ofrecer horarios disponibles y agendar
5. Si es información: responder lo que puedas; lo que no, agendar consulta

Reglas:
- Tono cálido, profesional, mexicano (no español de España)
- Máximo 3-4 líneas por respuesta
- Nunca prometas precios sin confirmar con el doctor
- Nunca confirmes diagnósticos médicos
- Si el paciente está alterado o tiene síntomas graves, deriva al doctor

Servicios principales: {lista_servicios}
Horario de atención: {horario}
```

Editar en `/configuracion/prompt` del cliente, o desde admin con `/admin/tenants/{id}/prompt`.

### Tipos de cita

En `/citas/tipos` (tenant), configurar:

- [ ] "Primera vez" — 45 min
- [ ] "Control" — 20 min
- [ ] "Urgencia" — 30 min (sin agendamiento previo)

Cada uno con su color en el calendario.

### Mensajes automáticos

Configurar en `/configuracion/automaticos` (tenant):

- [ ] Recordatorio 24h antes de la cita (default ON)
- [ ] Recordatorio 1h antes (default OFF — algunos clientes lo encuentran invasivo)
- [ ] Follow-up 24h después si no respondió a una propuesta
- [ ] Reactivación a 6 meses para pacientes inactivos

---

## Fase 4 — Capacitación del cliente

**Objetivo:** que el cliente sepa usar el dashboard y entienda qué hace el bot.

### Sesión de 30 min (videollamada)

Agenda sugerida:

1. **Recorrido del dashboard** (10 min)
   - Inbox de WhatsApp (chats en vivo)
   - Calendario de citas
   - Contactos / leads
   - Métricas básicas
   - Configuración

2. **Cómo intervenir en una conversación** (10 min)
   - Pausar el bot para un chat específico
   - Enviar mensaje manual
   - Reactivar el bot

3. **Cómo editar el system prompt** (5 min)
   - Solo si el plan incluye `allow_prompt_override=true`
   - Mostrar la página y explicar qué NO cambiar

4. **Q&A** (5 min)

### Material de soporte

Crear / mantener en `docs/cliente/`:
- `video-tour.md` — guion del video tutorial
- `faq-cliente.md` — preguntas frecuentes
- `como-conectar-google-calendar.md` — paso a paso con screenshots
- `como-obtener-token-whatsapp.md` — paso a paso con screenshots

---

## Fase 5 — Seguimiento (primeras 4 semanas)

**Objetivo:** que el cliente vea valor temprano y no churnee.

### Semana 1

- [ ] Día 1: confirmar primer mensaje procesado correctamente
- [ ] Día 3: check-in por WhatsApp: "¿todo bien?"
- [ ] Día 7: revisar métricas: ¿bot está calificando? ¿citas agendadas?

### Semana 2

- [ ] Revisar conversaciones donde el bot falló (paciente repitió, se confundió)
- [ ] Tunear el system prompt si hace falta
- [ ] Ajustar recordatorios si el cliente reporta queja

### Semana 3-4

- [ ] Reporte mensual (PDF) con:
  - Conversaciones atendidas
  - Citas agendadas
  - Tiempo promedio de respuesta
  - Leads calificados
- [ ] Llamada de feedback de 15 min

### Trigger de upsell

Si el cliente está en plan basic y:
- Tiene > 50 conversaciones/mes → ofrecer pro (calendar + follow-ups)
- Pide voz → ofrecer enterprise
- Pide intervención humana frecuente → ofrecer Chatwoot

---

## Fase 6 — Renovación / churn

### Renovación automática

Si `subscription_status='active'`, no hacer nada. El cliente sigue.

### Pago vencido

1. Al día 5 de impago: notificación al cliente vía WhatsApp.
2. Al día 10: pausar tenant (`/admin/tenants/{id}/pause`). El bot deja de responder.
3. Al día 30: marcar `is_active=false`. Las creds quedan, datos quedan.
4. Al día 90: borrar BD del tenant en Railway. Mantener fila en `tenants`
   para audit con `is_active=false`.

### Cancelación voluntaria

1. Cliente avisa → confirmar fecha de corte.
2. Exportar sus datos (CSV de contactos + conversaciones + citas) y mandárselos.
3. Pausar tenant en la fecha acordada.
4. Después de 30 días sin reactivar, mismo flujo de "pago vencido día 30+".

---

## Apéndice A — Cómo obtener el WhatsApp Token (para el cliente)

Pasos para el dueño del consultorio:

1. Ir a [developers.facebook.com](https://developers.facebook.com) → Login con la
   cuenta de Facebook que administra el WhatsApp Business.
2. **My Apps** → si ya tiene una app, abrirla. Si no:
   - Create App → Business → siguiente
   - Display name: "{Nombre del consultorio} WhatsApp"
   - App contact email: del owner
   - Business Account: la del consultorio (debe estar verificada en Meta Business)
3. En el app dashboard, **Add Product** → **WhatsApp** → Set up.
4. **API Setup** → seleccionar el número de teléfono (ya debe estar comprado
   y verificado en Meta Business).
5. **Generate token** → para producción usar System User token (no temporary):
   - Ir a [business.facebook.com/settings/system-users](https://business.facebook.com/settings/system-users)
   - Add → asignar la app de WhatsApp con permisos `whatsapp_business_messaging` + `whatsapp_business_management`
   - Generate Token → seleccionar la app → permisos arriba → never expires
6. **Phone Number ID:** en API Setup, junto al número.
7. **App Secret:** en Settings → Basic → "App Secret" (show).

El cliente nos manda los 3 valores. Nosotros los cargamos en el control plane.

---

## Apéndice B — Templates de comunicación

### Mensaje de bienvenida (post-onboarding)

```
¡Listo, Dr. {nombre}! Tu Korelabs ya está activo.

Desde ahora, los pacientes que escriban a tu WhatsApp van a ser atendidos
automáticamente por Kora, que califica, agenda y avisa.

Puedes ver todo en tiempo real en: https://dashboard.korelabs.app

Si necesitas pausar el bot para un chat específico o responder tú mismo,
también lo puedes hacer desde ahí.

Cualquier duda, aquí estoy 24/7.
```

### Reporte mensual (template)

```
{Mes} en {nombre del consultorio}:

📊 Métricas:
- Conversaciones: {n} (vs {n_prev} el mes pasado)
- Citas agendadas: {n}
- Tiempo promedio de respuesta: {seg} segundos
- Leads calificados: {n}

💰 Estimación de valor:
- Si cada cita representa ~$1,500 MXN → tu bot generó aprox ${valor} MXN
- ROI vs tu inversión mensual: {x}x

🚀 Recomendación del mes:
- {sugerencia específica basada en data}

Si quieres ajustar algo del bot o ver el detalle, aquí está tu panel:
https://dashboard.korelabs.app
```

---

*Última actualización: 2026-05-13 · Korelabs · Onboarding Playbook v1.0*
