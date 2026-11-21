from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIGHTSHIFT_", env_file=".env", extra="ignore", frozen=True
    )

    request_timeout: float = 30.0
    """Timeout in seconds for requests to outside services."""

    log_level: str = "INFO"
    """Loguru level: ``DEBUG``, ``INFO``, ``WARNING`` or ``ERROR``."""


SETTINGS = Settings()
