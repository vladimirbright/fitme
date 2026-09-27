"""`services/planning.py::import_plan` (IMPLEMENTATION_PLAN M8b "Paste my plan") against a
real temp DB and a `FunctionModel` LLM (no network): the operator's sample program imports
with every day and the right catalog ids, unmatched exercises are reported and never
invented, a no-history kg becomes calibration with the declared hint, an exercise with
history gets the engine's value when the declared load breaks the cap or ceiling, the draft
carries no `load_changes`, confirm counts once and writes `origin = 'import'`, an
output-validation failure escalates once, out-of-range declared loads are dropped, and
`declared_kg` never changes a guard verdict or the engine's output."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_planning_service import (
    _SQUAT,
    _settings,
    decisions,
    llm_calls,
    prescription,
    seed_completed_squat_session,
    seed_profile,
)

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.decisions import get_decision
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, RefusalCode
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    PlanImport,
    Prescription,
    Refusal,
    ScheduledDay,
    Workout,
)
from fitme.llm.agents import plan_import_agent, plan_revise_agent
from fitme.llm.usage import CAUSE_OUTPUT_VALIDATION
from fitme.services import planning
from fitme.services.llm_runtime import LlmRuntime

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_BENCH = "dumbbell_bench_press"  # per_implement, no history in these tests


def user_plan_text() -> str:
    return (_FIXTURES / "user_plan.txt").read_text(encoding="utf-8")


def user_plan_import() -> PlanImport:
    """The operator's sample program (`user_plan.txt`) transcribed the way the `plan_import`
    agent is asked to: every day, every exercise mapped to an allowed id, loads as written
    (lowest per set, lower bound of a range, durations as reps with the unit in `note`)."""
    return PlanImport.model_validate_json(
        (_FIXTURES / "user_plan_import.json").read_text(encoding="utf-8")
    )


# "Сессия 1 (Пн) … Присед со штангой — 80 × 5 × 3 …": a compact program for a home gym.
COMPACT_TEXT = """Сессия 1 (Пн)
Присед со штангой — 80 × 5 × 3
Отжимания — 3×12
Сессия 2 (Чт)
Жим гантелей лёжа — 30 × 8 × 3 (вес одной гантели)
"""


def compact_import(*, squat_kg: float = 80.0, bench_kg: float = 30.0) -> PlanImport:
    return PlanImport(
        plan=Plan(
            name="Моя программа",
            schedule=[
                ScheduledDay(weekday=0, workout_key="A"),
                ScheduledDay(weekday=3, workout_key="B"),
            ],
            workouts=[
                Workout(
                    key="A",
                    title="Сессия 1",
                    blocks=[
                        Block(
                            kind="single",
                            items=[prescription(_SQUAT, Load(kind="kg", kg=squat_kg), reps=(5, 5))],
                        ),
                        Block(
                            kind="single",
                            items=[prescription("pushup", Load(kind="bodyweight"), reps=(12, 12))],
                        ),
                    ],
                ),
                Workout(
                    key="B",
                    title="Сессия 2",
                    blocks=[
                        Block(
                            kind="single",
                            items=[prescription(_BENCH, Load(kind="kg", kg=bench_kg), reps=(8, 8))],
                        )
                    ],
                ),
            ],
        ),
        unmatched=[],
    )


# --- The fake LLM ------------------------------------------------------------------------


def _invalid_import_args() -> dict[str, object]:
    """A `PlanImport` tool call that fails pydantic validation on every output retry (an
    out-of-range weekday)."""
    return {
        "plan": {"name": "x", "schedule": [{"weekday": 9, "workout_key": "A"}], "workouts": []},
        "unmatched": [],
    }


@dataclass
class FakeImportLlm:
    """Answers each `plan_import`/`plan_revise` call with the next queued output. A queued
    `"invalid"` makes the model emit a structurally invalid `PlanImport` on every retry of that
    run, so the run fails output validation. `by_model` (optional) overrides the queue for one
    resolved model string — used to make the medium tier fail and the large tier succeed."""

    responses: list[PlanImport | Plan | Refusal | str] = field(default_factory=list)
    by_model: dict[str, PlanImport | str] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def _respond(self, model: str, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                self.prompts.append(str(part.content))
                self.models.append(model)
        output = self.by_model.get(model)
        if output is None:
            if not self.responses:
                # The provider path (A§8.5 rule 4): an `AgentRunError` subclass, as a real
                # HTTP/timeout failure would surface through pydantic-ai.
                raise ModelHTTPError(status_code=503, model_name=model, body=None)
            output = self.responses.pop(0)
        if output == "invalid":
            tool = next(t.name for t in info.output_tools if "Refusal" not in t.name)
            return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=_invalid_import_args())])
        assert not isinstance(output, str)
        wanted = "Refusal" if isinstance(output, Refusal) else "Plan"
        tool = next(
            t.name
            for t in info.output_tools
            if wanted in t.name and (wanted == "Refusal" or "Refusal" not in t.name)
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=output.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings | None = None) -> LlmRuntime:
        def factory(model: object) -> object:
            name = str(model)
            return FunctionModel(lambda m, i: self._respond(name, m, i), model_name=name)

        return LlmRuntime(
            settings=settings or _settings(),
            prices={},
            agent_factories={
                "plan_import": lambda model: plan_import_agent(factory(model)),
                "plan_revise": lambda model: plan_revise_agent(factory(model)),
            },
        )


# --- Seeding -------------------------------------------------------------------------------


async def seed_gym_profile(db: Database, user_id: int, *, sessions_per_week: int = 3) -> None:
    """A complete public-gym profile (every catalog implement available), every flag "no"."""
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="40_49",
            weight_bucket="120_129",
            experience="2y_5y",
            barbell_experience="yes",
            preferences=["weight_training"],
            location="public_gym",
            equipment=[],
            sessions_per_week=sessions_per_week,
            session_minutes=90,
            focus="strength",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS | AREA_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


def _prescriptions(plan: Plan) -> list[Prescription]:
    return [item for workout in plan.workouts for block in workout.blocks for item in block.items]


def _by_exercise(plan: Plan) -> dict[str, Prescription]:
    return {item.exercise_id: item for item in _prescriptions(plan)}


# --- The operator's sample program ---------------------------------------------------------


async def test_the_sample_program_imports_with_every_day_and_the_right_catalog_ids(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    expected = user_plan_import()
    llm = FakeImportLlm([expected])

    result = await planning.import_plan(db, llm.runtime(), user_id, user_plan_text())

    plan = result.plan
    assert plan is not None, result.refusal
    assert len(llm.prompts) == 1
    # The pasted text reached the model verbatim (scrubbed) with the allowed ids and history.
    sent = json.loads(llm.prompts[0])
    assert sent["imported_text"] == user_plan_text()
    assert "allowed_exercise_ids" in sent["context"]
    assert "telegram" not in llm.prompts[0].lower()
    # Every day and every exercise of the transcription, in order, with the same ids.
    assert [(d.weekday, d.workout_key) for d in plan.schedule] == [(0, "A"), (2, "B"), (4, "C")]
    assert [w.key for w in plan.workouts] == ["A", "B", "C"]
    assert [i.exercise_id for i in _prescriptions(plan)] == [
        i.exercise_id for i in _prescriptions(expected.plan)
    ]
    assert plan.workouts[0].blocks[3].kind == "superset"
    # No history at all: every declared kg became calibration with the declared hint kept.
    for wanted, got in zip(_prescriptions(expected.plan), _prescriptions(plan), strict=True):
        if wanted.load.kind == "kg":
            assert got.load == Load(kind="calibration"), got.exercise_id
            assert got.declared_kg == wanted.load.kg, got.exercise_id
        else:
            assert got.load == wanted.load and got.declared_kg is None, got.exercise_id
    assert result.unmatched == tuple(expected.unmatched)
    assert result.proposed_load_changes == []

    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_import"]
    draft = rounds[0]
    assert draft.id == result.decision_id
    assert draft.model == _settings().llm_tier_medium
    assert draft.prompt_template == "plan_import" and draft.prompt_version == "1"
    assert draft.llm_input == sent
    assert draft.load_changes == []  # A§4.3: a draft never counts toward the cap
    assert draft.proposal is not None
    assert draft.proposal["unmatched"] == expected.unmatched
    assert draft.proposal["declared_loads"] == {
        "barbell_back_squat": 80.0,
        "dumbbell_seated_shoulder_press": 20.0,
        "cable_close_grip_pulldown": 73.0,
        "dumbbell_seated_french_press": 25.0,
        "cable_rotation": 35.0,
        "barbell_romanian_deadlift": 67.5,
        "dumbbell_bench_press": 32.5,
        "cable_neutral_grip_pulldown": 61.0,
        "dumbbell_incline_curl": 10.0,
        "dumbbell_lateral_raise": 5.0,
        "barbell_bench_press": 85.0,
        "dumbbell_reverse_lunge": 17.5,
        "barbell_bent_over_row": 52.5,
        "dumbbell_incline_rear_delt_raise": 7.5,
    }
    assert Plan.model_validate(draft.proposal["plan"]) == plan
    calibrated = [g for g in result.guards_fired if g.rule == "loads.import_calibration"]
    assert len(calibrated) == 14 and all(g.ok for g in calibrated)
    assert not any(g.rule == "loads.substituted" for g in result.guards_fired)
    assert await planning.current_draft_id(db, user_id) == result.decision_id


async def test_confirmed_import_has_origin_import_and_keeps_the_declared_hints(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    result = await planning.import_plan(
        db, FakeImportLlm([user_plan_import()]).runtime(), user_id, user_plan_text()
    )
    assert result.plan is not None

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)

    assert confirmed.status == planning.ConfirmStatus.SAVED
    assert confirmed.version == 1 and confirmed.is_default
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, plans[0].id)
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
    assert plans[0].name == "Сентябрьский блок, финальная неделя"
    assert [v.origin for v in versions] == ["import"]
    assert versions[0].decision_id == confirmed.decision_id
    assert confirm_decision is not None and confirm_decision.kind == "plan_confirm"
    assert confirm_decision.load_changes == []  # calibration prescriptions add none
    stored = Plan.model_validate(versions[0].body)
    assert stored == result.plan
    assert _by_exercise(stored)[_SQUAT].declared_kg == 80.0
    assert _by_exercise(stored)[_SQUAT].load == Load(kind="calibration")

    # A double-tap is idempotent, and the draft is then stale for a revision.
    again = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert again.status == planning.ConfirmStatus.ALREADY_SAVED
    assert await planning.current_draft_id(db, user_id) is None


# --- Unmatched, loads with history, escalation, declared bounds ------------------------------


async def test_an_unmatched_exercise_is_reported_not_invented(db: Database, user_id: int) -> None:
    """The transcription leaves the exercise out and names it; nothing non-catalog reaches the
    plan, and a transcription that *does* invent an id is rejected, retried, then refused."""
    await seed_profile(db, user_id)
    reported = compact_import()
    reported.unmatched = ["Жим в тренажёре Смита"]
    llm = FakeImportLlm([reported])

    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)

    assert result.plan is not None
    assert result.unmatched == ("Жим в тренажёре Смита",)
    assert {i.exercise_id for i in _prescriptions(result.plan)} == {_SQUAT, "pushup", _BENCH}
    rounds = await decisions(db, user_id)
    assert rounds[-1].proposal is not None
    assert rounds[-1].proposal["unmatched"] == ["Жим в тренажёре Смита"]

    invented = compact_import()
    invented.plan.workouts[0].blocks.append(
        Block(kind="single", items=[prescription("smith_machine_press", Load(kind="calibration"))])
    )
    bad = FakeImportLlm([invented, invented, invented])
    refused = await planning.import_plan(db, bad.runtime(), user_id, COMPACT_TEXT)

    assert refused.refusal is not None and refused.refusal.code == RefusalCode.NO_SAFE_PLAN
    assert len(bad.prompts) == 3  # medium, medium with feedback, large (A§8.5 rule 3)
    assert "smith_machine_press" in bad.prompts[1] and "guard_feedback" in bad.prompts[1]
    assert bad.models == [
        _settings().llm_tier_medium,
        _settings().llm_tier_medium,
        _settings().llm_tier_large,
    ]
    kinds = [d.kind for d in await decisions(db, user_id)]
    assert kinds[-4:] == ["plan_import", "plan_import", "plan_import", "refusal"]


async def test_a_no_history_kg_becomes_calibration_with_the_declared_hint(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    llm = FakeImportLlm([compact_import(squat_kg=80.0, bench_kg=30.0)])

    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)

    assert result.plan is not None and len(llm.prompts) == 1
    by_id = _by_exercise(result.plan)
    assert by_id[_SQUAT].load == Load(kind="calibration") and by_id[_SQUAT].declared_kg == 80.0
    assert by_id[_BENCH].load == Load(kind="calibration") and by_id[_BENCH].declared_kg == 30.0
    assert by_id["pushup"].load == Load(kind="bodyweight") and by_id["pushup"].declared_kg is None
    # The M8b rule, not a guard rejection: no ceiling failure was needed to get there.
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("loads.import_calibration", True) in rules
    assert ("ceiling.historical_max", False) not in rules
    assert ("loads.substituted", True) not in rules


async def test_with_history_the_engine_value_replaces_a_declared_load_over_the_ceiling(
    db: Database, user_id: int
) -> None:
    """One completed squat session at 40 kg with every rep hit: the engine proposes 42.5, and
    the pasted plan's 80 kg breaks the ceiling (and the cap), so 42.5 is substituted — a load
    rule, no retry. The bench (no history) still becomes calibration with its hint."""
    await seed_profile(db, user_id)
    llm_first = FakeImportLlm([compact_import(squat_kg=40.0)])
    draft = await planning.import_plan(db, llm_first.runtime(), user_id, COMPACT_TEXT)
    confirmed = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED and confirmed.plan_version_id
    await seed_completed_squat_session(
        db, user_id, confirmed.plan_version_id, planned_kg=40.0, reps=8
    )
    llm = FakeImportLlm([compact_import(squat_kg=80.0)])

    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)

    assert result.plan is not None and len(llm.prompts) == 1
    by_id = _by_exercise(result.plan)
    assert by_id[_SQUAT].load == Load(kind="kg", kg=42.5)
    assert by_id[_SQUAT].declared_kg == 80.0  # recorded, not shown next to a kg load
    assert by_id[_BENCH].load == Load(kind="calibration") and by_id[_BENCH].declared_kg == 30.0
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("ceiling.historical_max", False) in rules
    assert ("loads.substituted", True) in rules
    assert [(c.exercise_id, c.from_kg, c.to_kg) for c in result.proposed_load_changes] == [
        (_SQUAT, 40.0, 42.5)
    ]
    rounds = await decisions(db, user_id)
    assert rounds[-1].kind == "plan_import" and rounds[-1].load_changes == []

    # Confirm counts the +2.5 exactly once, on the `plan_confirm` decision.
    saved = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert saved.status == planning.ConfirmStatus.SAVED and saved.version == 1
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, saved.decision_id or 0)
        versions = await list_plan_versions(conn, saved.plan_id or 0)
    assert confirm_decision is not None
    assert confirm_decision.load_changes == [
        {"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}
    ]
    assert versions[-1].origin == "import"


async def test_an_output_validation_failure_on_the_medium_tier_escalates_once_to_large(
    db: Database, user_id: int
) -> None:
    settings = _settings()
    await seed_profile(db, user_id)
    llm = FakeImportLlm(
        by_model={
            settings.llm_tier_medium: "invalid",
            settings.llm_tier_large: compact_import(),
        }
    )

    result = await planning.import_plan(db, llm.runtime(settings), user_id, COMPACT_TEXT)

    assert result.plan is not None
    calls = await llm_calls(db)
    assert [(c.model, c.ok) for c in calls] == [
        (settings.llm_tier_medium, False),
        (settings.llm_tier_large, True),
    ]
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_import", "plan_import"]
    assert rounds[0].proposal is not None
    assert rounds[0].proposal["cause"] == CAUSE_OUTPUT_VALIDATION
    assert rounds[0].proposal["refusal"]["code"] == "llm_unavailable"
    assert rounds[1].id == result.decision_id and rounds[1].model == settings.llm_tier_large


async def test_a_provider_error_refuses_without_escalation(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    llm = FakeImportLlm()  # an empty queue: the model call raises -> a provider error

    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)

    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.LLM_UNAVAILABLE
    assert len(await llm_calls(db)) == 1


async def test_gates_refuse_an_import_without_an_llm_call(db: Database, user_id: int) -> None:
    llm = FakeImportLlm([compact_import()])
    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)
    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.PROFILE_INCOMPLETE
    assert llm.prompts == []
    assert [d.kind for d in await decisions(db, user_id)] == ["refusal"]


async def test_an_out_of_range_declared_load_is_dropped(db: Database, user_id: int) -> None:
    """A§6.5.1 absolute bounds on the display hint: 500 kg on a barbell (> 300) and 70 kg per
    dumbbell (> 60) are typos, not hints. The prescription still becomes calibration."""
    await seed_profile(db, user_id)
    llm = FakeImportLlm([compact_import(squat_kg=500.0, bench_kg=70.0)])

    result = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)

    assert result.plan is not None
    by_id = _by_exercise(result.plan)
    assert by_id[_SQUAT].load == Load(kind="calibration") and by_id[_SQUAT].declared_kg is None
    assert by_id[_BENCH].load == Load(kind="calibration") and by_id[_BENCH].declared_kg is None
    rounds = await decisions(db, user_id)
    assert rounds[-1].proposal is not None and rounds[-1].proposal["declared_loads"] == {}


async def test_a_models_own_declared_kg_is_never_kept(db: Database, user_id: int) -> None:
    """`declared_kg` traces back only to a pasted plan's numbers: the import agent's own value
    is replaced by the transcribed load; a generated plan never has one; a revision restores
    the base draft's hints and cannot add new ones."""
    await seed_profile(db, user_id)
    imported = compact_import(squat_kg=80.0)
    imported.plan.workouts[0].blocks[0].items[0].declared_kg = 200.0
    imported.plan.workouts[0].blocks[1].items[0].declared_kg = 50.0  # a push-up
    llm = FakeImportLlm([imported])
    draft = await planning.import_plan(db, llm.runtime(), user_id, COMPACT_TEXT)
    assert draft.plan is not None
    assert _by_exercise(draft.plan)[_SQUAT].declared_kg == 80.0
    assert _by_exercise(draft.plan)["pushup"].declared_kg is None

    revised_plan = compact_import(squat_kg=80.0).plan
    revised_plan.name = "Revised"
    for item in _prescriptions(revised_plan):
        item.load = Load(kind="calibration")
        item.declared_kg = 123.0 if item.exercise_id == "pushup" else None
    revise_llm = FakeImportLlm([revised_plan])
    revised = await planning.revise_plan(
        db,
        revise_llm.runtime(),
        user_id,
        planning.DraftBase(decision_id=draft.decision_id),
        "rename it",
    )

    assert revised.plan is not None and revised.plan.name == "Revised"
    by_id = _by_exercise(revised.plan)
    assert by_id[_SQUAT].declared_kg == 80.0 and by_id[_BENCH].declared_kg == 30.0
    assert by_id["pushup"].declared_kg is None
    # The revision's draft is a `plan_revise` decision; confirming it is an `llm` version.
    confirmed = await planning.confirm_plan(db, _settings(), user_id, revised.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    async with db.read() as conn:
        versions = await list_plan_versions(conn, confirmed.plan_id or 0)
    assert versions[-1].origin == "llm"
    assert _by_exercise(Plan.model_validate(versions[-1].body))[_SQUAT].declared_kg == 80.0


async def test_declared_kg_never_changes_the_engine_or_the_load_changes(
    db: Database, user_id: int
) -> None:
    """Property-ish: for every prescription of a plan with history, the load engine's value
    and the load-change accounting are identical with and without a huge `declared_kg`."""
    await seed_profile(db, user_id)
    imported = compact_import(squat_kg=40.0)
    first = await planning.import_plan(
        db, FakeImportLlm([imported]).runtime(), user_id, COMPACT_TEXT
    )
    confirmed = await planning.confirm_plan(db, _settings(), user_id, first.decision_id)
    assert confirmed.plan_version_id is not None
    await seed_completed_squat_session(
        db, user_id, confirmed.plan_version_id, planned_kg=40.0, reps=8
    )
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    inputs = planning.build_inputs(load_catalog(), snapshot, _settings(), user_id)

    plain = compact_import(squat_kg=42.5).plan
    hinted = compact_import(squat_kg=42.5).plan
    for item in _prescriptions(hinted):
        item.declared_kg = 250.0
    assert planning.load_changes_for(plain, inputs.ctx) == planning.load_changes_for(
        hinted, inputs.ctx
    )
    for item in _prescriptions(plain):
        exercise = load_catalog().by_id(item.exercise_id)
        assert exercise is not None
        load, reason, _ = planning.engine_load(exercise, inputs)
        assert (load, reason) == planning.engine_load(exercise, inputs)[:2]
        assert math.isfinite(load.kg or 1.0)
    plain_judged = planning.judge(plain, inputs)
    hinted_judged = planning.judge(hinted, inputs)
    assert [(v.rule, v.ok, v.detail) for v in plain_judged.verdicts] == [
        (v.rule, v.ok, v.detail) for v in hinted_judged.verdicts
    ]
    assert [(v.rule, v.ok) for v in plain_judged.fired] == [
        (v.rule, v.ok) for v in hinted_judged.fired
    ]
    assert [i.load for i in _prescriptions(plain)] == [i.load for i in _prescriptions(hinted)]
    assert all(i.declared_kg == 250.0 for i in _prescriptions(hinted))  # untouched, unread
