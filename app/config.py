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
    openai_realtime_model: str = "gpt-realtime-2"

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
    # 'semantic_vad' es más robusto a eco/ruido y no dispara barge-in falso
    # en línea telefónica. 'server_vad' es más rápido a detectar fin de turno
    # pero ladra a cualquier ráfaga.
    realtime_vad_type: str = "semantic_vad"
    # Solo aplica a semantic_vad: low|medium|high. high responde más rápido
    # cuando el usuario termina (max 2s timeout vs 8s en low).
    realtime_vad_eagerness: str = "high"
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
    openai_prompt_id: str = ""

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
