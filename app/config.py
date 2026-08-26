"""
Application configuration and settings.
"""

import logging

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database. The name comes from DATABASE_NAME so a script can never carry
    # a hard-coded target: "bank_analyzer" belongs to the previous version of
    # this app and is still in use, so this project works against its own
    # database and must not write to that one.
    mongodb_url: str = "mongodb://localhost:27017"
    database_name: str = "bank_analyzer_dev"

    # Logging
    log_level: str = "INFO"

    # Classification via the Claude API. The key is read here and never
    # reaches a template or a response body; the frontend is server-rendered,
    # so there is no client bundle it could leak into.
    anthropic_api_key: str | None = None
    classification_model: str = "claude-haiku-4-5"
    #: A second pass for everything the first model was unsure about. Set to
    #: an empty string to switch escalation off. It also gets prompt caching
    #: for free: the cacheable prefix minimum is 4096 tokens on Haiku 4.5 and
    #: 1024 on Sonnet 5, and the system block sits between the two.
    classification_escalation_model: str = "claude-sonnet-5"
    #: Below this, a result goes to review instead of forcing a category.
    classification_confidence_threshold: float = 0.7
    #: Transactions per API request. Larger batches amortise the cached system
    #: prompt over more work; too large and one failure costs more to retry.
    classification_batch_size: int = 25
    #: Attempts per batch before it is given up on. A failed batch must not
    #: take the rest of the import down with it.
    classification_max_attempts: int = 3
    #: Where PayPal debits go when Comdirect truncated the merchant away.
    #: There is no merchant in those lines for any classifier to read, so they
    #: are filed deterministically rather than sent to the API. The category
    #: is knowingly approximate; classification_source marks them as such.
    #: The hand-written substring rules are switched off, not deleted. They
    #: served 2,676 transactions and 609 rules before the classifier replaced
    #: them; the engine, the rule library and the /categorize page all remain
    #: so the decision can be reversed by flipping this back on.
    rules_enabled: bool = False

    #: While the rule engine is still in place, log both verdicts for every
    #: imported transaction so the disagreements can be read before it is
    #: switched off.
    #: Only meaningful while rules_enabled is true — there is nothing to
    #: compare against once the rule engine no longer produces a verdict.
    comparison_mode_enabled: bool = True
    comparison_log_path: str = "logs/classification-comparison.log"
    #: Which system's verdict is actually stored. False keeps the rule engine
    #: in charge while the comparison log is being read.
    classification_enabled: bool = True

    paypal_fallback_major: str = "Shopping"
    paypal_fallback_minor: str = "General Merchandise"

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
