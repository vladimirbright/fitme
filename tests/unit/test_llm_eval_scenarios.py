"""ADR 0009: the `fitme llm eval` scenarios run deterministically with a fake model — the
fixtures' exclusions and requests reach the prompt, and plans are judged by the guards and
then the programming review. (`make llm-eval` runs the same fixtures against a real model.)"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.llm.eval import run_eval, summarize

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "llm_eval"


def _squat_plan() -> Plan:
    squat = Prescription(
        exercise_id="barbell_back_squat",
        sets=3,
        reps_min=8,
        reps_max=10,
        load=Load(kind="calibration"),
        rest_seconds=120,
    )
    return Plan(
        name="Plan",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[Workout(key="A", title="A", blocks=[Block(kind="single", items=[squat])])],
    )


async def test_the_scenarios_send_their_constraints_and_judge_the_plan() -> None:
    prompts: list[dict[str, object]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                prompts.append(json.loads(str(part.content)))
        tool = next(t.name for t in info.output_tools if "Refusal" not in t.name)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=_squat_plan().model_dump(mode="json"))]
        )

    results = await run_eval(
        agents=["plan_generate"],
        fixtures_dir=_FIXTURES,
        model_for_agent=lambda _name: FunctionModel(respond),
        model_name_for_agent=lambda name: f"fake:{name}",
        prices={},
    )

    by_name = {result.fixture_name: result for result in results}
    boxing = next(p for p in prompts if p.get("user_request", "").startswith("Силовая"))  # type: ignore[union-attr]
    context = boxing["context"]
    assert isinstance(context, dict)
    assert "barbell_back_squat" not in context["allowed_exercise_ids"]
    assert context["sport"] == "boxing_martial_arts" and context["exclusions"]
    assert {"power", "agility"} <= set(boxing["requested_qualities"])  # type: ignore[arg-type]
    # The same squat plan: excluded for the boxer, missing equipment in the hotel room, and
    # missing the requested hypertrophy declaration in the gym.
    assert by_name["scenario_a_boxing_support"].outcome == "guard_fail"
    assert "excluded" in by_name["scenario_a_boxing_support"].detail
    assert by_name["scenario_c_hotel"].outcome == "guard_fail"
    assert by_name["scenario_b_hypertrophy"].outcome == "findings"
    assert "review clean" in summarize(results)
