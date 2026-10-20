"""Typed application configuration, loaded once at startup.

Everything the app needs from the environment is declared here with a type and,
where sensible, a validator. The alternative — scattered ``os.getenv`` calls —
fails at 3am in the middle of a request instead of at boot.

Secrets are wrapped in :class:`~pydantic.SecretStr` so that an accidental
``repr()`` of the settings object in a log line prints ``**********`` rather than
a live Slack token.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Fail on unknown variables rather than silently ignoring a typo like
        # SLACK_BOT_TOKN. A typo'd secret name is a very confusing outage.
        extra="ignore",
    )

    # --- Slack -------------------------------------------------------------
    slack_bot_token: SecretStr
    slack_app_token: SecretStr
    slack_signing_secret: SecretStr = SecretStr("")

    # --- Anthropic ---------------------------------------------------------
    anthropic_api_key: SecretStr

    triage_cheap_model: str = "claude-haiku-4-5-20251001"
    triage_deep_model: str = "claude-sonnet-5"

    # Escalation thresholds. See triage.router for how these combine.
    triage_escalate_below_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    triage_escalate_at_urgency: int = Field(default=4, ge=1, le=5)

    # --- Database ----------------------------------------------------------
    database_url: str
    db_pool_min_size: int = Field(default=2, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)
    db_command_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- Privacy -----------------------------------------------------------
    content_hmac_master_key: SecretStr
    content_hmac_key_generation: str = "v1"

    # --- Application -------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_renderer: Literal["console", "json"] = "console"

    prompt_version: str = "v1"
    taxonomy_version: str = "v1"
    pipeline_version: str = "v1"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        """asyncpg rejects the SQLAlchemy-style ``postgresql+driver://`` prefix.

        People paste connection strings from all sorts of places, so normalize
        rather than making that a confusing connection error at startup.
        """
        if "+" in v.split("://", 1)[0]:
            scheme, rest = v.split("://", 1)
            v = f"{scheme.split('+', 1)[0]}://{rest}"
        return v

    @field_validator("content_hmac_master_key")
    @classmethod
    def _reject_placeholder_key(cls, v: SecretStr) -> SecretStr:
        """Refuse to start with the placeholder from ``.env.example``.

        Booting with a known key would make every fingerprint in the database
        forgeable, which defeats the entire point of using HMAC over SHA-256.
        """
        raw = v.get_secret_value()
        if "REPLACE-ME" in raw or len(raw) < 32:
            raise ValueError(
                "CONTENT_HMAC_MASTER_KEY is unset, placeholder, or too short. "
                "Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so validation runs exactly once and every module observes the same
    object. Call ``get_settings.cache_clear()`` in tests to re-read the env.
    """
    return Settings()  # type: ignore[call-arg]
