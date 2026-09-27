"""`fitme llm eval` (A§11, A§8.5 rule 7): CLI wiring around `llm/eval.py`'s business logic.

Real-money, human-run only. Never invoked by tests except with a fake model injected directly
into `llm.eval.run_eval` — `tests/unit/test_llm_eval.py` calls that function, not this one, so
no test ever resolves a real model string or makes a network call.
"""

from __future__ import annotations

from pathlib import Path

from fitme.config.settings import Settings, SettingsError
from fitme.llm import eval as llm_eval_module
from fitme.llm.agents import AGENT_FACTORIES, AgentModel
from fitme.llm.models import model_for, validate_startup
from fitme.llm.usage import load_price_table

# src/fitme/cli/llm_eval.py -> cli -> fitme -> src -> repo root. Fixtures live under
# tests/fixtures/llm_eval/ (A§11), not shipped in the wheel: this command is a dev tool meant
# to be run from a checkout, the same way `make llm-eval` runs it.
_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm_eval"

_WARNING = (
    "fitme llm eval makes real calls to a real LLM provider and spends real money. "
    "It is a manual tool: never run it in CI."
)


async def run(settings: Settings, *, agent: str | None, model: str | None, confirmed: bool) -> int:
    """Print the spend warning, then refuse without `--yes` (A§8.5 rule 7). `agent` limits
    the run to one agent name; `model` overrides tier resolution with one explicit model
    string for every agent run."""
    print(_WARNING)
    if not confirmed:
        print("Refusing to run without --yes.")
        return 1

    if agent is not None and agent not in AGENT_FACTORIES:
        print(f"unknown agent {agent!r}; must be one of {sorted(AGENT_FACTORIES)}")
        return 1
    agents = [agent] if agent is not None else sorted(AGENT_FACTORIES)

    try:
        validate_startup(settings)
    except SettingsError as exc:
        print(f"LLM configuration is invalid: {exc}")
        return 1

    if not _FIXTURES_DIR.is_dir():
        print(f"no fixtures directory at {_FIXTURES_DIR}")
        return 1

    prices = load_price_table(settings.prices_file)

    def resolve_model(agent_name: str) -> AgentModel:
        return model if model is not None else model_for(agent_name, settings).model

    results = await llm_eval_module.run_eval(
        agents=agents,
        fixtures_dir=_FIXTURES_DIR,
        model_for_agent=resolve_model,
        model_name_for_agent=lambda agent_name: str(resolve_model(agent_name)),
        prices=prices,
    )
    print(llm_eval_module.summarize(results))
    return 0
