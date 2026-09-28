"""Tests for fitme.config.settings (M0 acceptance: required vars, defaults, prefix, bounds)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from fitme.config.settings import Settings, SettingsError, agent_overrides, load_settings

# 32+ characters, as required by Settings.secret_key's min_length.
_VALID_SECRET_KEY = "test-secret-key-0123456789abcdef"

REQUIRED_ENV = {
    "FITME_TELEGRAM_BOT_TOKEN": "test-token",
    "FITME_DB_PATH": "./fitme-test.db",
    "FITME_WEB_BASE_URL": "https://fit.example.org",
    "FITME_SECRET_KEY": _VALID_SECRET_KEY,
}


@pytest.fixture(autouse=True)
def _clean_fitme_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate settings tests from any real .env file and the developer's shell env.

    Settings no longer reads a `.env` file at all (A§1: environment variables only), but we
    still chdir away from the repo so a stray `.env` in the working directory can never be a
    factor, now or if this ever regresses.
    """
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("FITME_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_missing_required_variable_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only two of the four required variables are set.
    monkeypatch.setenv("FITME_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FITME_DB_PATH", "./fitme-test.db")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "FITME_WEB_BASE_URL" in message
    assert "FITME_SECRET_KEY" in message


def test_defaults_apply_when_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    settings = Settings()

    assert settings.llm_tier_large == "anthropic:claude-opus-5"
    assert settings.llm_tier_medium == "anthropic:claude-sonnet-5"
    assert settings.llm_tier_small == "anthropic:claude-haiku-4-5"
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8080
    assert settings.forwarded_allow_ips == "127.0.0.1"
    assert settings.chat_retention_days == 365
    assert settings.max_weekly_increment_kg == 2.5
    assert settings.prices_file is None
    assert settings.dev is False
    assert settings.source_url == "https://github.com/vladimirbright/fitme"
    assert settings.llm_agent_overrides == {}


def test_env_prefix_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unprefixed variables must not satisfy the required fields.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unprefixed-token")
    monkeypatch.setenv("DB_PATH", "./fitme-test.db")
    monkeypatch.setenv("WEB_BASE_URL", "https://fit.example.org")
    monkeypatch.setenv("SECRET_KEY", "unprefixed-secret")

    with pytest.raises(SettingsError):
        load_settings()


def test_per_agent_override_is_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_GENERATE", "large")

    settings = Settings()

    assert settings.llm_agent_overrides == {"plan_generate": "large"}


def test_agent_override_named_overrides_does_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: FITME_LLM_AGENT_OVERRIDES used to be mistaken for the (now removed)
    # `overrides` model field and crash with an uncaught JSON-decode error. "overrides" is
    # just as valid an agent name as any other here.
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_LLM_AGENT_OVERRIDES", "large")

    settings = Settings()

    assert settings.llm_agent_overrides == {"overrides": "large"}


def test_agent_overrides_function_parses_a_given_mapping() -> None:
    result = agent_overrides(
        {"FITME_LLM_AGENT_PLAN_REVISE": "medium", "FITME_WEB_PORT": "8080", "OTHER": "x"}
    )

    assert result == {"plan_revise": "medium"}


def test_tier_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_LLM_TIER_SMALL", "anthropic:claude-haiku-3")

    settings = Settings()

    assert settings.llm_tier_small == "anthropic:claude-haiku-3"


def test_repr_does_not_leak_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)

    settings = Settings()
    rendered = repr(settings)

    assert "test-token" not in rendered
    assert _VALID_SECRET_KEY not in rendered


def test_short_secret_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_SECRET_KEY", "too-short")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    assert "FITME_SECRET_KEY" in str(exc_info.value)
    # The rejected value must not survive in a chained traceback (journald, stderr).
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


def test_empty_telegram_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_TELEGRAM_BOT_TOKEN", "")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    assert "FITME_TELEGRAM_BOT_TOKEN" in str(exc_info.value)


def test_web_port_out_of_range_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_WEB_PORT", "70000")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    assert "FITME_WEB_PORT" in str(exc_info.value)


def test_max_weekly_increment_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_MAX_WEEKLY_INCREMENT_KG", "0")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    assert "FITME_MAX_WEEKLY_INCREMENT_KG" in str(exc_info.value)


def test_chat_retention_days_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("FITME_CHAT_RETENTION_DAYS", "0")

    with pytest.raises(SettingsError) as exc_info:
        load_settings()

    assert "FITME_CHAT_RETENTION_DAYS" in str(exc_info.value)
