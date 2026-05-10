# korelabs_llamadas

Servicio de llamadas telefónicas atendidas por OpenAI Realtime, integrado al stack multi-tenant de Korelabs.

## Stack

- Python 3.11 + FastAPI (async)
- Twilio Programmable Voice + Media Streams (WebSocket)
- OpenAI Realtime API (`gpt-realtime`)
- PostgreSQL — comparte la BD del bot de WhatsApp del tenant (contacto unificado por `wa_id`)
- Hosting: Railway

## Cómo funciona

1. Lead marca el número Twilio del tenant.
2. Twilio envía un webhook HTTP a `POST /twilio/voice/incoming`.
3. El servicio resuelve el `tenant_id` a partir del número marcado, registra la llamada en la BD del tenant y devuelve TwiML que abre un `<Connect><Stream/>` hacia `WSS /twilio/media-stream`.
4. Sobre esa WebSocket se hace de puente bidireccional con la WebSocket de OpenAI Realtime.
5. Audio en `g711_ulaw` end-to-end (sin transcoding), transcripts persistidos a Postgres con `pg_notify` para que el dashboard los vea en vivo.

## Setup local

```bash
cp .env.example .env   # llenar variables
pip install -r requirements.txt
uvicorn main:app --reload
curl http://localhost:8000/healthz
```

## Despliegue

Railway, auto-deploy desde `main`. `Procfile` arranca uvicorn en `$PORT`.

## Repos relacionados

- [`Gustavomezz/korelabs-whatsapp-bot`](https://github.com/Gustavomezz/korelabs-whatsapp-bot) — bot de WhatsApp
- [`Gustavomezz/Dashboard_clientesKorelabs`](https://github.com/Gustavomezz/Dashboard_clientesKorelabs) — dashboard multi-tenant
