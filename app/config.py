"""
Application configuration and settings.
"""

import logging

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    mongodb_url: str = "mongodb://localhost:27017"
    database_name: str = "bank_analyzer"

    # Logging
    log_level: str = "INFO"

    # Comdirect API (optional)
    comdirect_startup_auth_enabled: bool = False
    comdirect_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMDIRECT_CLIENT_ID", "CLIENT_ID"),
    )
    comdirect_client_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMDIRECT_CLIENT_SECRET", "CLIENT_SECRET"),
    )
    comdirect_access_number: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMDIRECT_ACCESS_NUMBER", "ACCESS_NUMBER"),
    )
    comdirect_pin: str | None = Field(
        default=None,
        validation_alias=AliasChoices("COMDIRECT_PIN", "PIN"),
    )


settings = Settings()


def get_logger(name: str) -> logging.Logger:
    """Get a configured logger instance."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(settings.log_level)
    return logger
