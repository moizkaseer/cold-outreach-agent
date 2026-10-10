import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_and_secret_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    s = Settings(_env_file=None)  # ignore any real .env on the dev machine

    assert s.model_smart == "claude-sonnet-5-5"
    assert s.model_fast == "claude-haiku-4-5-20251001"
    assert s.research_max_turns == 10
    # The key must never leak via repr/logging.
    assert "sk-test-123" not in repr(s)
    assert s.anthropic_api_key.get_secret_value() == "sk-test-123"


def test_missing_api_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_overrides_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEARCH_MAX_TURNS", "5")
    assert Settings(_env_file=None).research_max_turns == 5

    monkeypatch.setenv("RESEARCH_MAX_TURNS", "0")  # below ge=1
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value", "says"),
    [
        ("REDIS_URL", "", "REDIS_URL must start with redis://, but it is empty"),
        ("REDIS_URL", "postgresql://u:hunter2@h/db", "starts with postgresql://"),
        ("REDIS_URL", "${{Redis.REDIS_URL}}", "is not a URL"),
        ("DATABASE_URL", "postgresql://u:hunter2@h/db", "add +asyncpg"),
        ("DATABASE_URL", "", "DATABASE_URL must start with"),
    ],
)
def test_bad_urls_fail_at_startup_naming_the_variable(monkeypatch, name, value, says) -> None:
    # First deploy: an unresolved Railway reference gave an empty REDIS_URL and the only
    # clue was arq's "invalid DSN scheme". The message must name the setting, and must
    # not leak the value (these URLs carry passwords and errors land in host logs).
    from pydantic import ValidationError

    from app.config import InfraSettings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as err:
        InfraSettings(_env_file=None)
    message = str(err.value).replace(" or rediss://", "")
    assert says in message and "hunter2" not in message


@pytest.mark.parametrize("url", ["redis://default:pw@redis.railway.internal:6379",
                                 "rediss://x:y@host:6380/0"])  # fmt: skip
def test_good_redis_urls_pass(monkeypatch, url) -> None:
    from app.config import InfraSettings

    monkeypatch.setenv("REDIS_URL", url)
    assert InfraSettings(_env_file=None).redis_url == url
