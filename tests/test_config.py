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
