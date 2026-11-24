from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NIGHTSHIFT_", env_file=".env", extra="ignore", frozen=True
    )

    request_timeout: float = 30.0
    """Timeout in seconds for requests to outside services."""

    log_level: str = "INFO"
    """Loguru level: ``DEBUG``, ``INFO``, ``WARNING`` or ``ERROR``."""

    home: Path | None = None
    """The folder that holds this instance's config, database, lock, logs and worktrees
    (``NIGHTSHIFT_HOME``). Each folder is a separate instance. Unset: ``~/.nightshift``."""


SETTINGS = Settings()
