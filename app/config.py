import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("korelabs.llamadas")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Proveedor de voz: 'openai' (default) o 'grok' (NO RECOMENDADO).
    # Grok Voice Think Fast probado en mayo 2026: latencia alta,
    # conversation.item.truncate no soportado (barge-in roto), VAD se
    # confunde con eco. Default OpenAI Realtime hasta que xAI mejore.
    voice_provider: str = "openai"

    openai_api_key: str = ""
    # gpt-realtime-2: modelo flagship con razonamiento GPT-5-class. Más
    # robusto contra transcripciones ambiguas (descarta inputs que no
    # tienen sentido en lugar de procesarlos a ciegas). ~10x más caro que
    # mini pero mejor para flows con stakes (calificación + agendamiento
    # de citas reales con clientes pagantes).
    # Para volver a mini: setear OPENAI_REALTIME_MODEL=gpt-realtime-mini.
    openai_realtime_model: str = "gpt-realtime-2"

    # Grok / xAI. Solo se usan si voice_provider='grok'.
    xai_api_key: str = ""
    xai_realtime_model: str = "grok-voice-think-fast-1.0"
    # Voz Grok: eve | ara | rex | sal | leo. ara es femenina cálida (más
    # parecida al estilo de "Kora" de OpenAI/cedar).
    grok_voice: str = "ara"
    # 'minimal' (más rápido) | 'low' | 'medium' | 'high' | 'xhigh'.
    # CRÍTICO: con effort 'medium' o más alto, gpt-realtime-2 hace
    # "internal reasoning" y genera MÚLTIPLES output items por response —
    # i.e., el modelo auto-simula varios turnos de la conversación en
    # uno solo, incluyendo roleplayear lo que el usuario "diría". Eso
    # produce 5 burbujas Kora seguidas y respuestas a inputs hallucinados.
    # 'minimal' fuerza one-shot generation: una sentence y para.
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
    # threshold: el MISMO threshold gatea tanto el inicio de un nuevo turno
    # como el barge-in (interrupción mientras el bot habla). No hay forma
    # de separarlos en server_vad. Tradeoff:
    # - 0.5 (default OpenAI): barge-in funciona bien, pero VAD dispara con
    #   ruido ambiente/breathing → falsos positivos durante silencio.
    # - 0.7+: pocos falsos positivos, pero voz débil por teléfono no llega
    #   a interrumpir al bot.
    # 0.7 reduce falsos positivos de ruido ambiente / breathing. La
    # interrupción no se resuelve aflojando este threshold sino con
    # response.cancel explícito desde nuestro código (ver audio_bridge).
    # silence_duration_ms: principal driver de latencia turn-by-turn.
    # prefix_padding_ms: cuánto audio "antes" del speech incluye en el buffer.
    realtime_vad_threshold: float = 0.7
    realtime_vad_silence_ms: int = 300
    realtime_vad_prefix_ms: int = 200
    # ms mínimos de audio enviado antes de respetar un evento de barge-in.
    # Sirve de guard contra eco inmediato del bot que la VAD detecta como
    # speech del caller. 500ms es un punto seguro empíricamente con
    # threshold 0.7.
    barge_in_guard_ms: int = 500
    # ms a ignorar la VAD DESPUÉS de que el bot terminó de hablar. El audio
    # del bot por speaker genera reverb que llega 200-800ms después del fin
    # del response.done. Sin esto, el server VAD escucha el reverb, lo
    # transcribe como "user speech", el bot responde a su propio eco y la
    # conversación se vuelve loca. 1500ms para teléfono en altavoz con
    # transcriber prone a alucinaciones cortas tipo "Gustavo" o "Claro".
    post_speech_guard_ms: int = 1500
    # HALF-DUPLEX MODE: workaround creado para Grok cuando VAD no separaba
    # eco de voz real. Con OpenAI Realtime el VAD nativo + barge-in con
    # response.cancel + truncate funciona bien, así que NO se necesita
    # activarlo. Disponible como fallback si en algún cliente con speaker
    # malo sigue habiendo eco — pero no debería ser necesario.
    half_duplex_mode: bool = False
    # Pool de WebSockets pre-conectadas a OpenAI. Cada conexión idle ahorra
    # ~500 ms de TCP+TLS+upgrade en cold start. 0 deshabilita el pool.
    realtime_ws_pool_size: int = 2
    # Server-stored prompt ID en OpenAI (creado vía POST /v1/prompts). Si
    # está set, lo usamos en lugar de mandar instructions inline. Reduce
    # payload y maximiza cache hit del prompt en OpenAI.
    # IMPORTANTE: solo aplica para modelos v2 (gpt-realtime-2+).
    # mini y 1.5 lo ignoran (usan instructions inline siempre).
    openai_prompt_id: str = ""

    # CRÍTICO: límite duro para prevenir que el modelo encadene varios
    # output items dentro de una response. Con 400 el modelo puede generar
    # ~5 sentences seguidas (Paso 4a + 4b + roleplay del user + repetir
    # correo + etc). Con 80 físicamente le alcanza para UNA pregunta
    # corta y se corta. Forzar one-sentence-per-turn por límite duro.
    openai_max_output_tokens: int = 80

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

    # WhatsApp Cloud API (mismas credenciales que el bot de WA del tenant).
    # Se usa para mandar el link de Google Meet al caller después de que
    # el bot agende. Si están vacías, el envío se salta silenciosamente y
    # el correo de Google Calendar sigue siendo el canal principal.
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    graph_api_version: str = "v21.0"


settings = Settings()
