"""`LlmRuntime`: the process-wide, read-only bundle a service needs to run an agent (A§8):
the `Settings` (tier resolution, A§8.5), the price table (`llm_calls.cost_estimate_usd`,
A§8.5 rule 5) and the agent factories.

Lives in `services/`, not `llm/`, because it's what the front-ends hand to a service
(A§2.1: `bot/`/`web/`/`cli/` import `services/`, never `llm/`); `llm/` itself never needs
it. Built once at startup (`from_settings`) and handed to the bot dispatcher / web app, which
pass it into `services/`. Tests build one with `agent_factories` overridden by factories that
ignore the resolved model string and wrap a `pydantic_ai.models.function.FunctionModel`, so
the whole `/plan` flow runs end to end with no network and no provider credentials, while
the *model name* recorded on `decisions.model`/`llm_calls.model` is still the resolved string
(the FunctionModel is labelled with it).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fitme.config.settings import Settings
from fitme.llm.agents import AGENT_FACTORIES, AgentModel, BuiltAgent
from fitme.llm.usage import PriceTable, load_price_table

AgentFactory = Callable[[AgentModel], BuiltAgent[Any]]


@dataclass(frozen=True, slots=True)
class LlmRuntime:
    settings: Settings
    prices: PriceTable = field(default_factory=dict)
    agent_factories: Mapping[str, AgentFactory] = field(default_factory=lambda: AGENT_FACTORIES)

    @classmethod
    def from_settings(cls, settings: Settings) -> LlmRuntime:
        return cls(settings=settings, prices=load_price_table(settings.prices_file))

    def factory(self, agent_name: str) -> AgentFactory:
        try:
            return self.agent_factories[agent_name]
        except KeyError:
            raise KeyError(f"no agent factory registered for {agent_name!r}") from None
