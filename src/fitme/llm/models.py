"""Model tier resolution (A§8.5) exactly as specified there:

- three tiers (`large`/`medium`/`small`), each defaulting to a model string from `Settings`;
- a fixed, in-code mapping from agent name to its default tier;
- per-agent `ModelSettings`, also fixed in code;
- `model_for(agent, settings) -> ModelSpec`: `FITME_LLM_AGENT_<NAME>` (a tier name or a full
  model string) wins over the agent's default tier.

Validated at startup (A§8.5 rule 1), without any network call or provider instantiation: an
unknown agent name in an override, an unknown tier, a model string pydantic-ai's own parsing
can't recognize a provider for, or (B3) a missing API-key environment variable for a
*configured* provider, all fail fast via `validate_startup`. Model-string parsing uses
`pydantic_ai.models.parse_model_id` + `pydantic_ai.providers.infer_provider_class` — the
latter only imports the provider's module and returns its class, never instantiates it, so it
needs no API key and makes no request itself (constructing a real `Provider`, which
`infer_model` also does, is what would need `ANTHROPIC_API_KEY` etc. — deliberately avoided
here). The API-key check (B3) is a plain `provider -> env var name(s)` lookup and
`os.environ` read — still no instantiation, still no network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic_ai.models import parse_model_id
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.settings import ModelSettings

from fitme.config.settings import Settings, SettingsError

TIERS: tuple[str, ...] = ("large", "medium", "small")

# B3: a small, deliberately incomplete provider -> API-key env var map, covering the
# providers Fitme ships defaults for (anthropic) plus the other ones an operator is most
# likely to configure. A provider *not* in this map is simply not checked here — this is a
# best-effort fail-fast for the common case, not a substitute for the provider's own error
# when it actually tries to authenticate (google accepts either env var, legacy GEMINI_API_KEY
# included, matching `pydantic_ai.providers.google.GoogleProvider`).
_PROVIDER_API_KEY_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}

# A§8.1 table: the agent -> default tier mapping. Env vars only override this; the mapping
# itself lives in code (A§8.5 rule 1).
AGENT_DEFAULT_TIER: dict[str, str] = {
    "plan_generate": "large",
    "plan_revise": "medium",
    "session_adjust": "medium",
    "result_parse": "small",
    "recap": "small",
}

# A§8.5 rule 6: per-agent ModelSettings, in code. Kept to cross-provider `ModelSettings` keys
# (`max_tokens`, `timeout`) rather than a provider-specific key (e.g. Anthropic's own
# reasoning-effort field) — ADR 0001/A§8.5 rule 6 says to look up exact provider-specific
# keys when they're actually needed, not guess them; these three agents don't need one yet.
# `plan_generate` gets the largest budget and longest timeout (rare, large-model, whole-plan
# calls); `result_parse`/`recap` the smallest (frequent, small-model, short-output calls).
AGENT_MODEL_SETTINGS: dict[str, ModelSettings] = {
    "plan_generate": ModelSettings(max_tokens=4096, timeout=120),
    "plan_revise": ModelSettings(max_tokens=2048, timeout=60),
    "session_adjust": ModelSettings(max_tokens=1024, timeout=60),
    "result_parse": ModelSettings(max_tokens=512, timeout=30),
    "recap": ModelSettings(max_tokens=512, timeout=30),
}


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A resolved model string plus the settings to run it with (A§8.5 `model_for`)."""

    model: str
    settings: ModelSettings


def _tier_model_string(tier: str, settings: Settings) -> str:
    if tier not in TIERS:
        raise SettingsError(f"unknown model tier {tier!r}; must be one of {TIERS}")
    return {
        "large": settings.llm_tier_large,
        "medium": settings.llm_tier_medium,
        "small": settings.llm_tier_small,
    }[tier]


def validate_model_string(model_string: str) -> None:
    """Fail fast on a model string pydantic-ai's own parsing can't recognize a provider for
    (A§8.5 rule 1), without a network call or provider credentials (see module docstring)."""
    provider_name, model_name = parse_model_id(model_string)
    if provider_name is None or not model_name:
        raise SettingsError(
            f"{model_string!r} is not a valid pydantic-ai model string "
            "(expected 'provider:model', e.g. 'anthropic:claude-sonnet-5')"
        )
    try:
        infer_provider_class(provider_name)
    except ValueError as exc:
        raise SettingsError(str(exc)) from None


def _check_api_key_present(model_string: str) -> None:
    """B3: fail fast if `model_string`'s provider is one we know an API-key env var for
    (`_PROVIDER_API_KEY_ENV`) and none of its candidate env vars is set. A provider outside
    that small map is silently skipped — this is a friendly, best-effort check, not a
    complete substitute for the provider's own error at call time."""
    provider_name, _model_name = parse_model_id(model_string)
    if provider_name is None:
        return  # already reported by validate_model_string
    env_vars = _PROVIDER_API_KEY_ENV.get(provider_name)
    if env_vars is None:
        return
    if not any(os.environ.get(var) for var in env_vars):
        wanted = " or ".join(env_vars)
        raise SettingsError(
            f"model {model_string!r} uses provider {provider_name!r}, but {wanted} is not "
            "set in the environment"
        )


def resolve_tier(tier: str, settings: Settings) -> str:
    """The configured model string for `tier`, validated. Raises `SettingsError` for an
    unknown tier name or an unparseable configured model string."""
    model_string = _tier_model_string(tier, settings)
    validate_model_string(model_string)
    return model_string


def model_for(agent: str, settings: Settings) -> ModelSpec:
    """A§8.5 `model_for`: `FITME_LLM_AGENT_<NAME>` wins if set (a tier name or a full model
    string); otherwise the agent's default tier, resolved through `FITME_LLM_TIER_*`."""
    if agent not in AGENT_DEFAULT_TIER:
        raise SettingsError(f"unknown agent {agent!r}; must be one of {sorted(AGENT_DEFAULT_TIER)}")
    override = settings.llm_agent_overrides.get(agent)
    if override is not None:
        model_string = resolve_tier(override, settings) if override in TIERS else override
        if override not in TIERS:
            validate_model_string(model_string)
    else:
        model_string = resolve_tier(AGENT_DEFAULT_TIER[agent], settings)
    return ModelSpec(model=model_string, settings=AGENT_MODEL_SETTINGS[agent])


def model_settings_for(agent: str) -> ModelSettings:
    """The static per-agent `ModelSettings`, independent of which model/tier is resolved.
    Used by `llm/agents.py`'s factories, which take a model but not a full `ModelSpec` (so
    tests can inject a `TestModel`/`FunctionModel` without going through tier resolution at
    all)."""
    if agent not in AGENT_MODEL_SETTINGS:
        raise SettingsError(f"unknown agent {agent!r}; must be one of {sorted(AGENT_DEFAULT_TIER)}")
    return AGENT_MODEL_SETTINGS[agent]


def validate_startup(settings: Settings) -> None:
    """Everything A§8.5 rule 1 (plus B3) requires fail fast at startup, in one call:

    - every `FITME_LLM_TIER_*` default is a parseable model string;
    - every known agent resolves cleanly (covers a per-agent tier-name override and a
      per-agent full-model-string override);
    - every `FITME_LLM_AGENT_<NAME>` override actually names a known agent — this is what
      catches the empty name from `FITME_LLM_AGENT_=x` (`config.settings.agent_overrides`
      strips the prefix down to `""`, which is never a real agent name) and any other typo;
    - (B3) every distinct resolved model string has its provider's API-key env var set, for
      the providers `_PROVIDER_API_KEY_ENV` knows about.

    Called once, by the CLI, before anything that would make a real model call.
    """
    resolved_models: set[str] = set()
    for tier in TIERS:
        resolved_models.add(resolve_tier(tier, settings))
    for agent in AGENT_DEFAULT_TIER:
        resolved_models.add(model_for(agent, settings).model)
    unknown_agents = sorted(set(settings.llm_agent_overrides) - set(AGENT_DEFAULT_TIER))
    if unknown_agents:
        raise SettingsError(
            "unknown agent name(s) in FITME_LLM_AGENT_* overrides: "
            + ", ".join(repr(name) for name in unknown_agents)
        )
    for model_string in sorted(resolved_models):
        _check_api_key_present(model_string)
