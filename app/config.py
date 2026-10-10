"""Typed application settings, loaded from environment variables / `.env`.

Every tunable (model IDs, budgets, keys) lives here so nothing is hard-coded in agent
logic. Add a field here when a step actually needs it — not before.
"""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _describe(value: str) -> str:
    """Say what is wrong with a URL setting without revealing it (it holds a password)."""
    if not value.strip():
        return "is empty"
    scheme = value.split("://", 1)[0] if "://" in value else None
    return f"starts with {scheme}://" if scheme and len(scheme) < 30 else "is not a URL"


class InfraSettings(BaseSettings):
    """Database + Redis URLs only, so migrations and worker setup need no API keys."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unrelated vars in .env shouldn't crash startup
        # Validation errors normally echo the bad value. These values are URLs with
        # passwords in them, and errors end up in the host's logs.
        hide_input_in_errors=True,
    )

    # Local default needs no setup; production sets a Postgres URL, e.g.
    # postgresql+asyncpg://user:pass@host:5432/postgres (Supabase).
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # Arq job queue. Production: the managed Redis URL from Railway/Render/Fly.
    redis_url: str = "redis://localhost:6379"

    # Fail at startup with a message that names the variable. Without this a bad value
    # surfaces later as a library error ("invalid DSN scheme") that doesn't say which
    # setting is wrong; that cost a debugging round on the first deploy (2026-10-10).
    @field_validator("redis_url")
    @classmethod
    def _redis_url_shape(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError(
                f"REDIS_URL must start with redis:// or rediss://, but it {_describe(v)}. "
                "On Railway set it to ${{<name of your Redis service>.REDIS_URL}}; an empty "
                "value means no service with that exact name was found."
            )
        return v

    @field_validator("database_url")
    @classmethod
    def _database_url_shape(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "sqlite+aiosqlite://")):
            raise ValueError(
                f"DATABASE_URL must start with postgresql+asyncpg://, but it {_describe(v)}. "
                "Supabase shows postgresql://...; add +asyncpg after postgresql."
            )
        return v

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

    # --- Sending (app/api/sending.py). Off unless all three are set.
    resend_api_key: SecretStr | None = None
    send_from_email: str | None = None  # an address on a domain verified with the provider
    public_base_url: str | None = None  # where this API is reachable; used in unsubscribe links
    sends_per_key_per_day: int = Field(default=30, ge=0)  # low volume on purpose; 0 = no cap

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
