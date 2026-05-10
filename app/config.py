import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("korelabs.llamadas")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    openai_realtime_model: str = "gpt-realtime"

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
