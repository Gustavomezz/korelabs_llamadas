import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("korelabs.llamadas")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Proveedor de voz: 'openai' (gpt-realtime-2) o 'grok' (grok-voice-think-fast-1.0).
    # Cambia el endpoint, el envelope del session.update y los nombres de
    # algunos eventos. El bridge maneja ambos transparentemente.
    voice_provider: str = "openai"

    openai_api_key: str = ""
    # Default 'gpt-realtime-mini': benchmark real mostró 448ms median
    # session→audio vs 558ms de gpt-realtime-2 (110ms más rápido) y 5x más
    # barato. Para vender Korelabs (calificación + agendamiento) la
    # capacidad de razonamiento de v2 no es necesaria; mini la cubre
    # con sobra. Para casos que necesiten más razonamiento subir a
    # 'gpt-realtime-2'.
    openai_realtime_model: str = "gpt-realtime-mini"

    # Grok / xAI. Solo se usan si voice_provider='grok'.
    xai_api_key: str = ""
    xai_realtime_model: str = "grok-voice-think-fast-1.0"
    # Voz Grok: eve | ara | rex | sal | leo. ara es femenina cálida (más
    # parecida al estilo de "Kora" de OpenAI/cedar).
    grok_voice: str = "ara"
    # 'minimal' (más rápido, suficiente para calificación de leads) | 'low' |
    # 'medium' | 'high' | 'xhigh'. Default minimal: ahorra ~450ms en
    # tiempo a primer audio comparado con 'low'.
    openai_reasoning_effort: str = "minimal"
    # 'server_vad' detecta fin de turno por silencio en ms (configurable
    # abajo). 'semantic_vad' usa modelo NLU pero tiene timeout mínimo ~1-2s
    # incluso con eagerness=high. Para baja latencia preferimos server_vad
    # con threshold alto (anti-eco) + silence corto (~300ms) — corta turnos
    # 700-1700 ms más rápido que semantic_vad.
    realtime_vad_type: str = "server_vad"
    # Solo aplica a semantic_vad: low|medium|high.
    realtime_vad_eagerness: str = "high"
    # Solo aplica a server_vad.
    # threshold: 0.5 (default OpenAI) detecta voz normal por teléfono. Subirlo
    # a 0.85+ ignora voz baja (también la del caller cuando intenta
    # interrumpir al bot). El anti-eco del bot lo cubre BARGE_IN_GUARD_MS,
    # NO el threshold — por eso 0.5 es lo correcto incluso para teléfono.
    # silence_duration_ms: principal driver de latencia turn-by-turn.
    # prefix_padding_ms: cuánto audio "antes" del speech incluye en el buffer.
    realtime_vad_threshold: float = 0.5
    realtime_vad_silence_ms: int = 300
    realtime_vad_prefix_ms: int = 200
    # ms mínimos de audio enviado antes de respetar un evento de barge-in.
    # Sirve de guard contra eco inmediato del bot que la VAD detecta como
    # speech del caller. 500ms es un punto seguro empíricamente.
    barge_in_guard_ms: int = 500
    # Pool de WebSockets pre-conectadas a OpenAI. Cada conexión idle ahorra
    # ~500 ms de TCP+TLS+upgrade en cold start. 0 deshabilita el pool.
    realtime_ws_pool_size: int = 2
    # Server-stored prompt ID en OpenAI (creado vía POST /v1/prompts). Si
    # está set, lo usamos en lugar de mandar instructions inline. Reduce
    # payload y maximiza cache hit del prompt en OpenAI.
    # IMPORTANTE: solo aplica para modelos v2 (gpt-realtime-2+).
    # mini y 1.5 lo ignoran (usan instructions inline siempre).
    openai_prompt_id: str = ""

    # Limita el tamaño de las respuestas del modelo. En voz cortas son
    # mejor (menos tiempo del bot hablando, menos latencia total por
    # response). 400 cubre 30-40s de voz natural. Bajar a 200 fuerza
    # respuestas muy concisas.
    openai_max_output_tokens: int = 400

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""

    dashboard_database_url: str = ""
    tenant_db_encryption_key: str = ""

    public_base_url: str = ""
    port: int = 8000

    admin_token: str = ""

    # Google Calendar (compartidas con el bot de WhatsApp del tenant). Solo
    # las usamos para refrescar tokens; el OAuth inicial lo hace el bot WA.
    google_client_id: str = ""
    google_client_secret: str = ""
    calendar_timezone: str = "America/Mexico_City"


settings = Settings()
