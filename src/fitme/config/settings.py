"""Application configuration.

Everything is read from environment variables with the ``FITME_`` prefix (A§3.1). Config is
environment variables only: no `.env` file is loaded by this module (see `.env.example` for
how to load one at the shell/process level). This module only stores the configured values;
resolving a model tier or an agent override into an actual model string happens in
`llm/models.py` (M4).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, PrivateAttr, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError as PydanticSettingsSourceError

# Per-agent model overrides arrive as FITME_LLM_AGENT_<NAME> env vars, one per agent. The
# agent name is arbitrary, so it can't be a declared pydantic field: pydantic-settings only
# looks up env vars for fields it already knows about, and env_prefix stripping would make
# a field literally named `overrides` collide with FITME_LLM_AGENT_OVERRIDES. These are
# collected by a plain function instead, and stored on a private attribute (never a model
# field), so they never take part in env-source field matching.
_AGENT_OVERRIDE_ENV_PREFIX = "FITME_LLM_AGENT_"


def agent_overrides(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Parse FITME_LLM_AGENT_<NAME> overrides out of an environment mapping.

    Defaults to the real process environment. Keyed by the lowercased agent name, e.g.
    `FITME_LLM_AGENT_PLAN_GENERATE=large` becomes `{"plan_generate": "large"}`.
    """
    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for key, value in source.items():
        if key.upper().startswith(_AGENT_OVERRIDE_ENV_PREFIX) and value:
            agent_name = key.upper()[len(_AGENT_OVERRIDE_ENV_PREFIX) :].lower()
            result[agent_name] = value
    return result


class SettingsError(RuntimeError):
    """Configuration is missing or invalid. The message is meant to be read by a human."""


class Settings(BaseSettings):
    """Fitme configuration, read from environment variables only."""

    model_config = SettingsConfigDict(
        env_prefix="FITME_",
        case_sensitive=False,
    )

    # --- required (A§3.1) ---
    telegram_bot_token: SecretStr = Field(min_length=1)
    db_path: Path
    web_base_url: str
    secret_key: SecretStr = Field(min_length=32)

    # --- model tiers (A§8.5) ---
    llm_tier_large: str = "anthropic:claude-opus-5"
    llm_tier_medium: str = "anthropic:claude-sonnet-5"
    llm_tier_small: str = "anthropic:claude-haiku-4-5"

    # --- web server ---
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8080, ge=1, le=65535)
    # M10: passed to uvicorn as `forwarded_allow_ips` (with `proxy_headers=True`) so
    # `X-Forwarded-*` headers from a trusted reverse proxy (e.g. the optional `caddy` compose
    # service) are honored. Default trusts only the loopback peer, which is what a
    # non-proxied, `127.0.0.1`-published deployment sees. Never set this to `*` while the app
    # port is reachable from anything other than a trusted proxy (M10 compose notes).
    forwarded_allow_ips: str = "127.0.0.1"

    # --- retention & guards ---
    chat_retention_days: int = Field(default=365, ge=1)
    max_weekly_increment_kg: float = Field(default=2.5, gt=0, le=10)
    # ADR 0003: route unprompted free text to the `assistant` agent (one LLM call per
    # message). Off: such text gets the old "use the menu" hint.
    assistant_enabled: bool = True

    # --- misc ---
    prices_file: Path | None = None
    dev: bool = False
    source_url: str = "https://github.com/vladimirbright/fitme"

    # Populated in model_post_init from FITME_LLM_AGENT_<NAME> env vars. Not a model field:
    # see the module docstring / _AGENT_OVERRIDE_ENV_PREFIX comment above for why.
    _llm_agent_overrides: dict[str, str] = PrivateAttr(default_factory=dict)

    @property
    def llm_agent_overrides(self) -> dict[str, str]:
        return self._llm_agent_overrides

    def model_post_init(self, __context: object) -> None:
        self._llm_agent_overrides = agent_overrides()


def load_settings() -> Settings:
    """Build Settings, failing fast with a readable error on missing or invalid config."""
    try:
        return Settings()
    except ValidationError as exc:
        missing: list[str] = []
        invalid: list[str] = []
        for error in exc.errors():
            # Only the top-level field name maps to an env var; deeper loc parts (e.g. a
            # dict key or list index) don't, so they are dropped rather than glued on.
            field_name = str(error["loc"][0])
            env_var = f"FITME_{field_name.upper()}"
            if error["type"] == "missing":
                missing.append(env_var)
            else:
                invalid.append(f"{env_var}: {error['msg']}")
        lines = ["Fitme configuration is invalid."]
        if missing:
            lines.append("Missing required environment variables: " + ", ".join(missing))
        if invalid:
            lines.append("Invalid values: " + "; ".join(invalid))
        lines.append("See .env.example for the full list of variables.")
        raise SettingsError("\n".join(lines)) from None
    except PydanticSettingsSourceError as exc:
        raise SettingsError(
            f"Fitme configuration is invalid: {exc}\nSee .env.example for the full list "
            "of variables."
        ) from None
