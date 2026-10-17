"""`fitme llm eval-conversations` (experiment: llm-first planning): CLI wiring around
`llm/eval_conversations.py`.

Real-money, human-run only, like `fitme llm eval`. Tests call
`llm.eval_conversations.run_conversations` with `FunctionModel` factories, never this
module, so no test resolves a real model string or makes a network call.
"""

from __future__ import annotations

import os
from pathlib import Path

from fitme.config.settings import Settings, SettingsError
from fitme.llm import eval_conversations
from fitme.llm.models import validate_startup
from fitme.services.llm_runtime import LlmRuntime

# src/fitme/cli/llm_eval_conversations.py -> cli -> fitme -> src -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "llm_eval" / "conversations"
_REPORTS_DIR = _REPO_ROOT / "docs" / "reports"

_WARNING = (
    "fitme llm eval-conversations makes real calls to a real LLM provider and spends real "
    "money (every scenario, every turn, in each mode). It is a manual tool: never run it in CI."
)


async def run(
    settings: Settings,
    *,
    mode: str,
    scenario: str | None,
    model: str | None,
    confirmed: bool,
) -> int:
    """Print the spend warning, then refuse without `--yes`. `mode` is `planner`, `legacy`
    or `both`; `scenario` limits the run to one fixture name; `model` resolves every agent
    to one explicit model string."""
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    print(_WARNING)
    if not confirmed:
        print("Refusing to run without --yes.")
        return 1

    modes = [item for item in eval_conversations.MODES if mode in (item, "both")]
    if not modes:
        print(f"unknown mode {mode!r}; must be planner, legacy or both")
        return 1

    if model is not None:
        settings = eval_conversations.with_model_override(settings, model)
    try:
        validate_startup(settings)
    except SettingsError as exc:
        print(f"LLM configuration is invalid: {exc}")
        return 1

    if not _FIXTURES_DIR.is_dir():
        print(f"no fixtures directory at {_FIXTURES_DIR}")
        return 1
    names = [
        fixture.name for fixture in eval_conversations.load_conversation_fixtures(_FIXTURES_DIR)
    ]
    if scenario is not None and scenario not in names:
        print(f"unknown scenario {scenario!r}; must be one of {names}")
        return 1

    results = await eval_conversations.run_conversations(
        fixtures_dir=_FIXTURES_DIR,
        modes=modes,
        llm=LlmRuntime.from_settings(settings),
        scenario=scenario,
    )
    print(eval_conversations.render_summary(results))
    path = eval_conversations.write_report(results, _REPORTS_DIR)
    print(f"Report with full transcripts: {path}")
    return 0
