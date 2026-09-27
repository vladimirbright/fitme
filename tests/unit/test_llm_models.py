"""`llm/models.py` (A§8.5): tier defaults, per-agent overrides, and startup validation."""

from __future__ import annotations

import pytest

from fitme.config.settings import Settings, SettingsError
from fitme.llm import models


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="x",
        db_path="./fitme-test.db",
        web_base_url="https://fit.example.org",
        secret_key="x" * 32,
    )


def test_agent_default_tier_applies_when_nothing_is_overridden() -> None:
    settings = _settings()
    spec = models.model_for("plan_generate", settings)
    assert spec.model == settings.llm_tier_large
    spec = models.model_for("plan_revise", settings)
    assert spec.model == settings.llm_tier_medium
    spec = models.model_for("result_parse", settings)
    assert spec.model == settings.llm_tier_small


def test_per_agent_tier_override_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_REVISE", "large")
    settings = _settings()
    assert settings.llm_agent_overrides == {"plan_revise": "large"}

    spec = models.model_for("plan_revise", settings)
    assert spec.model == settings.llm_tier_large


def test_per_agent_full_model_string_override_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_RESULT_PARSE", "anthropic:claude-sonnet-4-5")
    settings = _settings()

    spec = models.model_for("result_parse", settings)
    assert spec.model == "anthropic:claude-sonnet-4-5"


def test_model_settings_are_agent_specific() -> None:
    settings = _settings()
    large_spec = models.model_for("plan_generate", settings)
    small_spec = models.model_for("result_parse", settings)
    assert large_spec.settings != small_spec.settings
    assert large_spec.settings["max_tokens"] > small_spec.settings["max_tokens"]


def test_unknown_agent_fails_fast() -> None:
    with pytest.raises(SettingsError, match="unknown agent"):
        models.model_for("not_a_real_agent", _settings())


def test_unknown_tier_override_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_REVISE", "extra_large")  # not a known tier
    settings = _settings()

    with pytest.raises(SettingsError, match="not a valid pydantic-ai model string"):
        models.model_for("plan_revise", settings)


def test_unparseable_model_string_override_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_REVISE", "not-a-provider-string")
    settings = _settings()

    with pytest.raises(SettingsError, match="not a valid pydantic-ai model string"):
        models.model_for("plan_revise", settings)


def test_unknown_provider_in_a_full_model_string_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_REVISE", "not-a-real-provider:some-model")
    settings = _settings()

    with pytest.raises(SettingsError, match="Unknown provider"):
        models.model_for("plan_revise", settings)


def test_empty_agent_name_override_fails_fast_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FITME_LLM_AGENT_=x` strips down to an empty agent name (`config.settings.
    agent_overrides`), which is never a real agent — `validate_startup` must catch it, not
    silently ignore it."""
    monkeypatch.setenv("FITME_LLM_AGENT_", "x")
    settings = _settings()
    assert settings.llm_agent_overrides == {"": "x"}

    with pytest.raises(SettingsError, match="unknown agent name"):
        models.validate_startup(settings)


def test_unknown_agent_name_override_fails_fast_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FITME_LLM_AGENT_NOT_A_REAL_AGENT", "large")
    settings = _settings()

    with pytest.raises(SettingsError, match="unknown agent name"):
        models.validate_startup(settings)


def test_unparseable_tier_default_fails_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FITME_LLM_TIER_SMALL", "not-a-provider-string")
    settings = _settings()

    with pytest.raises(SettingsError, match="not a valid pydantic-ai model string"):
        models.validate_startup(settings)


def test_validate_startup_passes_with_default_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    models.validate_startup(_settings())  # must not raise


# --- B3: a missing provider API-key env var fails startup fast, without any network call or
# provider instantiation. ---


def test_validate_startup_fails_fast_when_the_api_key_env_var_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = _settings()  # every default tier resolves to an anthropic: model

    with pytest.raises(SettingsError, match="ANTHROPIC_API_KEY"):
        models.validate_startup(settings)


def test_validate_startup_passes_once_the_api_key_env_var_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    models.validate_startup(_settings())  # must not raise


def test_check_api_key_present_accepts_the_legacy_gemini_api_key_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercised directly against `_check_api_key_present` (not the full `validate_startup`
    -> `model_for` -> `infer_provider_class` path): that path imports the provider's own
    client SDK (e.g. `google-genai`), which isn't installed in this project (only the
    `anthropic` extra is, per `pyproject.toml`) and would raise `ImportError` for reasons
    unrelated to what B3 is testing here."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")
    models._check_api_key_present("google:gemini-2.5-flash")  # must not raise


def test_check_api_key_present_skips_an_unmapped_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider outside the small `_PROVIDER_API_KEY_ENV` map (B3: deliberately
    incomplete) is not checked at all — no false failure just because Fitme doesn't know
    that provider's env var name. See the docstring above for why this goes straight at
    `_check_api_key_present` rather than through `validate_startup`."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    models._check_api_key_present("groq:llama-3.1-8b-instant")  # must not raise


def test_check_api_key_present_makes_no_network_call_and_no_provider_instantiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for B3's own promise: this must be a pure env-var read. If it ever
    started constructing a real `Provider`, missing credentials would raise `UserError`
    instead of `SettingsError`, and this would need a live network stub to pass."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SettingsError):
        models._check_api_key_present("anthropic:claude-opus-5")


def test_resolve_tier_rejects_an_unknown_tier_name() -> None:
    with pytest.raises(SettingsError, match="unknown model tier"):
        models.resolve_tier("extra_large", _settings())


def test_every_agent_has_model_settings() -> None:
    for agent in models.AGENT_DEFAULT_TIER:
        assert models.model_settings_for(agent)


def test_every_agent_has_an_output_retry_budget() -> None:
    """Bug fix: every agent `llm/agents.py::_built` constructs needs a budget to pass as
    `Agent(retries={"output": ...})` — pydantic-ai's own default (1) is what let two
    over-long display-text fields in a row exhaust the retry budget and turn a structurally
    fine plan into a bare `LLM_UNAVAILABLE` refusal."""
    for agent in models.AGENT_DEFAULT_TIER:
        assert models.output_retries_for(agent) >= 2


def test_escalating_agents_get_the_largest_output_retry_budget() -> None:
    """`plan_generate`/`plan_revise`/`session_adjust` (large/medium-tier, structural output)
    get 3; `result_parse`/`recap` (small-tier, frequent, short output) get 2."""
    for agent in ("plan_generate", "plan_revise", "session_adjust"):
        assert models.output_retries_for(agent) == 3
    for agent in ("result_parse", "recap"):
        assert models.output_retries_for(agent) == 2


def test_output_retries_for_unknown_agent_fails_fast() -> None:
    with pytest.raises(SettingsError, match="unknown agent"):
        models.output_retries_for("not_a_real_agent")
