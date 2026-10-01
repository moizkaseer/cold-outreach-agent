"""Typed application settings, loaded from environment variables / `.env`.

Every tunable (model IDs, budgets, keys) lives here so nothing is hard-coded in agent
logic. Add a field here when a step actually needs it — not before.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unrelated vars in .env shouldn't crash startup
    )

    # --- Anthropic ---
    # SecretStr keeps the key out of logs/reprs; call .get_secret_value() to use it.
    anthropic_api_key: SecretStr

    # Sonnet: research + writing (judgment-heavy). Haiku: scoring, classification,
    # verification (cheap, fast, high volume).
    model_smart: str = "claude-sonnet-5-5"
    model_fast: str = "claude-haiku-4-5-20251001"

    # --- Research agent budget (hard limits on the autonomous loop) ---
    research_max_turns: int = Field(default=10, ge=1, le=30)
    research_max_tokens: int = Field(default=60_000, ge=1_000)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide Settings instance (env is read once)."""
    return Settings()  # type: ignore[call-arg]  # values come from env
