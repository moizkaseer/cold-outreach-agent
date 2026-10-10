"""Typed application settings, loaded from environment variables / `.env`.

Every tunable (model IDs, budgets, keys) lives here so nothing is hard-coded in agent
logic. Add a field here when a step actually needs it — not before.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class InfraSettings(BaseSettings):
    """Database + Redis URLs only, so migrations and worker setup need no API keys."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unrelated vars in .env shouldn't crash startup
    )

    # Local default needs no setup; production sets a Postgres URL, e.g.
    # postgresql+asyncpg://user:pass@host:5432/postgres (Supabase).
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # Arq job queue. Production: the managed Redis URL from Railway/Render/Fly.
    redis_url: str = "redis://localhost:6379"

    # Tracing (Logfire). Unset = traces stay in-process; nothing is sent anywhere.
    logfire_token: SecretStr | None = None

    # --- API access (app/api/deps.py)
    # Off by default: every request needs an API key. The public demo turns this on so
    # visitors can create (rate-limited) runs without one.
    allow_anonymous_runs: bool = False
    key_runs_per_hour: int = Field(default=60, ge=0)  # per API key; 0 = unlimited
    # Browser origins allowed to call the API, comma-separated, e.g.
    # "https://moizali.vercel.app". Empty = no cross-origin browser access.
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> tuple[str, ...]:
        return tuple(o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip())

    # --- Public demo guards (app/api/limits.py). 0 disables a guard.
    demo_runs_per_client_per_hour: int = Field(default=3, ge=0)
    # Estimated USD per UTC day across all runs. Keep it at or below what you're willing
    # to lose if the demo is hammered; it is also worth setting a limit in the Anthropic
    # console as a second, provider-side backstop.
    demo_daily_spend_usd: float = Field(default=2.0, ge=0)
    # Worst-case cost assumed for each run still in flight (its spend isn't in the DB yet).
    demo_reserve_per_run_usd: float = Field(default=0.50, gt=0)


class Settings(InfraSettings):
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
