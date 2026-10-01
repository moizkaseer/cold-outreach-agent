"""Typed application settings, loaded from environment variables / `.env`.

Every tunable (model IDs, budgets, keys) lives here so nothing is hard-coded in agent
logic. Add a field here when a step actually needs it — not before.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Just the database URL, so migrations can run without any API keys."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unrelated vars in .env shouldn't crash startup
    )

    # Local default needs no setup; production sets a Postgres URL, e.g.
    # postgresql+asyncpg://user:pass@host:5432/postgres (Supabase).
    database_url: str = "sqlite+aiosqlite:///./dev.db"


class Settings(DatabaseSettings):
    # --- Anthropic ---
    # SecretStr keeps the key out of logs/reprs; call .get_secret_value() to use it.
    anthropic_api_key: SecretStr

    # Sonnet: research + writing (judgment-heavy). Haiku: scoring, classification,
    # verification (cheap, fast, high volume).
    model_smart: str = "claude-sonnet-5-5"
    model_fast: str = "claude-haiku-4-5-20251001"

    # --- Search (Exa). Optional so unit tests and non-search code run without it;
    # the research agent fails fast if it's missing.
    exa_api_key: SecretStr | None = None

    # --- Research agent budget (hard limits on the autonomous loop) ---
    research_max_turns: int = Field(default=10, ge=1, le=30)
    # Counts ALL input tokens across turns (history is re-sent each turn), so it must cover
    # ~8 turns of growing context. ~150k ≈ $0.30 worst case on Sonnet 5.5 before caching.
    research_max_tokens: int = Field(default=150_000, ge=1_000)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide Settings instance (env is read once)."""
    return Settings()  # type: ignore[call-arg]  # values come from env
