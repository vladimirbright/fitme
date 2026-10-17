"""The conversation eval harness (`llm/eval_conversations.py`, experiment: llm-first planning)
runs deterministically with scripted models: every shipped conversation fixture in both
modes against a real migrated temp DB, the checks pass and fail on hand-made plans as they
should, and the report carries the tables, the transcripts and the closing paragraph.
(`make llm-eval-conversations` runs the same fixtures against a real model.)"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme.catalog import load_catalog
from fitme.cli import llm_eval_conversations
from fitme.config.settings import Settings
from fitme.db.connection import open_database
from fitme.db.migrate import migrate
from fitme.domain.assistant import AssistantTurn
from fitme.domain.enums import Location, Measure
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.llm.agents import plan_generate_agent, plan_revise_agent, planner_agent
from fitme.llm.eval_conversations import (
    CLOSING_PARAGRAPH,
    LEGACY_NO_LOCATION,
    LEGEND,
    ConversationFixture,
    ConversationResult,
    Expect,
    Observed,
    evaluate,
    load_conversation_fixtures,
    render_report,
    run_conversation,
    run_conversations,
    with_model_override,
    write_report,
)
from fitme.llm.models import AGENT_DEFAULT_TIER, TIERS, model_for, resolve_tier
from fitme.services.llm_runtime import LlmRuntime

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "llm_eval" / "conversations"


# --- Plans --------------------------------------------------------------------------------------


def _timed(exercise_id: str, seconds: int, *, name: str | None = None) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        activity_name=name,
        sets=1,
        measure=Measure.TIME,
        seconds=seconds,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
    )


def _reps(exercise_id: str, load: Load) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=3, reps_min=6, reps_max=8, load=load, rest_seconds=120
    )


def _single(item: Prescription) -> Block:
    return Block(kind="single", items=[item])


def _run(*, location: Location | None, jog: int = 1500) -> Plan:
    return Plan(
        name="Saturday run",
        location=location,
        schedule=[ScheduledDay(weekday=5, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Easy run",
                blocks=[
                    _single(_timed("brisk_walk", 600)),
                    _single(_timed("easy_jog", jog)),
                    _single(_timed("brisk_walk", 300)),
                ],
            )
        ],
    )


def _bjj() -> Plan:
    def day(key: str) -> Workout:
        bjj = _timed("activity:bjj_class", 5400, name="BJJ class")
        return Workout(key=key, title="BJJ", blocks=[_single(bjj)])

    return Plan(
        name="BJJ",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=3, workout_key="B"),
        ],
        workouts=[day("A"), day("B")],
    )


def _strength(bench: Prescription) -> Plan:
    def day(key: str, items: list[Prescription]) -> Workout:
        return Workout(key=key, title=f"Day {key}", blocks=[_single(i) for i in items])

    return Plan(
        name="Three days",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=2, workout_key="B"),
            ScheduledDay(weekday=4, workout_key="C"),
        ],
        workouts=[
            day("A", [_reps("barbell_back_squat", Load(kind="kg", kg=80)), bench]),
            day("B", [_reps("dumbbell_row", Load(kind="calibration"))]),
            day("C", [_reps("pushup", Load(kind="bodyweight"))]),
        ],
    )


_BARBELL_BENCH = _reps("barbell_bench_press", Load(kind="kg", kg=60))
_DUMBBELL_BENCH = _reps("dumbbell_bench_press", Load(kind="calibration"))


# --- The scripted models ------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


def _stage(plan: Plan) -> Call:
    return Call("stage_plan", {"plan": plan.model_dump(mode="json"), "plan_id": None})


def _say(text: str) -> AssistantTurn:
    return AssistantTurn(message=text)


Step = Call | AssistantTurn


@dataclass
class Script:
    """The planner's steps per message and the legacy agents' plan per request text, keyed
    by the user's words as they reach the prompt (`user_request`)."""

    planner: dict[str, list[Step]] = field(default_factory=dict)
    plans: dict[str, Plan] = field(default_factory=dict)
    queue: list[Step] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        text = None
        for part in request.parts:
            if part.part_kind == "user-prompt":
                text = json.loads(str(part.content)).get("user_request")
        if any(tool.name == "stage_plan" for tool in info.function_tools):
            if text is not None:
                self.requests.append(text)
                self.queue = list(self.planner[text])
            step = self.queue.pop(0)
            if isinstance(step, Call):
                return ModelResponse(parts=[ToolCallPart(tool_name=step.name, args=step.args)])
            names = [tool.name for tool in info.output_tools]
            wanted = "final_result_AssistantTurn" if len(names) > 1 else names[0]
            return ModelResponse(
                parts=[ToolCallPart(tool_name=wanted, args=step.model_dump(mode="json"))]
            )
        assert text is not None
        self.requests.append(text)
        tool = next(t.name for t in info.output_tools if "Refusal" not in t.name)
        plan = self.plans[text]
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=plan.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings) -> LlmRuntime:
        def wrap(factory: Any) -> Any:
            return lambda model: factory(FunctionModel(self.respond, model_name=str(model)))

        return LlmRuntime(
            settings=settings,
            prices={},
            agent_factories={
                "planner": wrap(planner_agent),
                "plan_generate": wrap(plan_generate_agent),
                "plan_revise": wrap(plan_revise_agent),
            },
        )


def _script() -> Script:
    """A plausible answer for every turn of every shipped fixture."""
    script = Script()
    for fixture in load_conversation_fixtures(_FIXTURES):
        texts = [turn.user for turn in fixture.turns]
        if fixture.name.startswith("a_"):
            staged = _run(location=Location.OUTDOOR)
            script.planner.update(
                {
                    texts[0]: [_say("Walk 10 min, easy jog 25 min, walk 5 min. Shall I draft it?")],
                    texts[1]: [_stage(staged), _say("Staged.")],
                    texts[2]: [_stage(staged), _say("There is no jump rope in it.")],
                    texts[3]: [_stage(staged), _say("No dumbbells.")],
                    texts[4]: [Call("save_draft"), _say("Saved.")],
                }
            )
            for text in (texts[0], texts[2], texts[3]):
                script.plans[text] = _run(location=None)
        elif fixture.name == "d_hour_run":
            script.planner[texts[0]] = [_say("Steady, or with walking breaks?")]
            script.planner[texts[1]] = [_stage(_run(location=None, jog=2700)), _say("Staged.")]
            script.plans[texts[0]] = _run(location=None, jog=2700)
        elif fixture.name.startswith("e_"):
            script.planner.update(
                {
                    texts[0]: [_say("Just the two classes, or sessions around them?")],
                    texts[1]: [_stage(_bjj()), _say("Two classes.")],
                    texts[2]: [Call("save_draft"), _say("Saved.")],
                }
            )
            script.plans[texts[0]] = _bjj()  # an activity item: the legacy agent may not
        else:
            script.planner.update(
                {
                    texts[0]: [_say("Squat, bench, rows, push-ups over three days. Draft it?")],
                    texts[1]: [_stage(_strength(_BARBELL_BENCH)), _say("Staged.")],
                    texts[2]: [_stage(_strength(_DUMBBELL_BENCH)), _say("Swapped.")],
                    texts[3]: [Call("save_draft"), _say("Saved.")],
                }
            )
            script.plans[texts[0]] = _strength(_BARBELL_BENCH)
            script.plans[texts[2]] = _strength(_DUMBBELL_BENCH)
    return script


# --- The run ------------------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST-token-for-unit-tests-only",
        db_path=tmp_path / "unused.db",
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
    )


@pytest.fixture
async def results(settings: Settings) -> list[ConversationResult]:
    return await run_conversations(
        fixtures_dir=_FIXTURES, modes=["planner", "legacy"], llm=_script().runtime(settings)
    )


def _failed(result: ConversationResult) -> list[str]:
    return [
        f"turn {turn.index + 1} {check.name}: {check.detail}"
        for turn in result.turns
        for check in turn.checks
        if check.outcome == "fail" and not check.soft
    ]


async def test_every_fixture_runs_in_both_modes_and_is_judged(
    results: list[ConversationResult],
) -> None:
    names = [fixture.name for fixture in load_conversation_fixtures(_FIXTURES)]
    assert [(r.scenario, r.mode) for r in results] == [
        (name, mode) for name in names for mode in ("planner", "legacy")
    ]
    by_key = {(r.scenario, r.mode): r for r in results}
    for result in results:
        assert result.error is None, (result.scenario, result.mode, result.error)
        assert result.checks_total > 0
        if result.mode == "planner":
            assert all(turn.model_calls > 0 for turn in result.turns)

    # The planner did what each scenario asks: every check passes.
    for name in names:
        planner = by_key[(name, "planner")]
        assert _failed(planner) == [], (name, _failed(planner))
        assert all(turn.error is None for turn in planner.turns)

    # A question passes in the planner but is only reported: the any_of it can satisfy is
    # soft, so the scenario's weight is on the next turn's plan checks.
    first = by_key[("e_bjj_twice", "planner")].turns[0].checks
    assert [(c.name, c.outcome, c.soft) for c in first] == [("any_of", "pass", True)]
    assert "alternative 1 (question)" in first[0].detail
    asked = by_key[("d_hour_run", "planner")].turns
    assert [(c.name, c.soft) for c in asked[0].checks] == [("any_of", True)]
    assert {c.name for c in asked[1].checks if not c.soft} == {
        "draft",
        "exclude_kinds",
        "include_ids_any",
    }
    hour = by_key[("d_hour_run", "legacy")].turns[0]
    assert hour.plan is not None and hour.staged
    assert any(c.name == "cardio_minutes" and c.soft for c in hour.checks)

    # The legacy pipeline: a plan location is n/a (it cannot carry one) and the outdoor
    # check passes; F/G passes; an activity item is refused, so the revision has no draft
    # (recorded, not raised) and nothing is saved.
    legacy_run = by_key[("a_easy_run_saturdays", "legacy")]
    assert _failed(legacy_run) == []
    location = [c for t in legacy_run.turns for c in t.checks if c.name == "location"]
    assert location and {(c.outcome, c.detail) for c in location} == {("n/a", LEGACY_NO_LOCATION)}
    outdoors = [c for t in legacy_run.turns for c in t.checks if c.name == "outdoor_allowed"]
    assert outdoors and {c.outcome for c in outdoors} == {"pass"}
    assert _failed(by_key[("f_g_strength", "legacy")]) == []
    bjj = by_key[("e_bjj_twice_en", "legacy")]
    assert "refusal: no_safe_plan" in bjj.turns[0].events
    assert bjj.turns[1].error is not None and "no current draft" in bjj.turns[1].error
    assert bjj.turns[2].reply == "(no draft to save)"
    assert any("saved" in failure for failure in _failed(bjj))
    activity = [c for t in bjj.turns for c in t.checks if c.name == "activity_items_min"]
    assert activity and {c.outcome for c in activity} == {"n/a"}

    # Each mode runs only its own agents.
    for result in results:
        purposes = {p for turn in result.turns for p in turn.purposes}
        if result.mode == "planner":
            assert purposes == {"planner"}, result.scenario
        else:
            assert purposes <= {"plan_generate", "plan_revise"} and purposes, result.scenario
    assert {p for t in by_key[("f_g_strength", "legacy")].turns for p in t.purposes} == {
        "plan_generate",
        "plan_revise",
    }

    # Saved plans are what was staged; the swap kept every other item.
    strength = by_key[("f_g_strength", "planner")]
    swap = {c.name: c for c in strength.turns[2].checks}
    assert swap["single_change"].outcome == "pass"
    assert swap["single_change"].detail == "out: barbell_bench_press; in: dumbbell_bench_press"
    assert swap["swap_load"].detail == (
        "dumbbell_bench_press: calibration (as staged, before the guards)"
    )
    assert swap["history_loads"].outcome == "pass"
    assert strength.turns[3].plan is not None


async def test_the_report_has_the_tables_the_transcripts_and_the_closing_words(
    results: list[ConversationResult], tmp_path: Path
) -> None:
    when = datetime(2026, 10, 17, 9, 5, 7)
    path = write_report(results, tmp_path / "reports", now=when)
    assert path == tmp_path / "reports" / "llm_first_eval_2026-10-17_090507.md"
    report = path.read_text(encoding="utf-8")
    assert report == render_report(results, title_time=when)
    assert LEGEND in report and "a planner run with N tool round-trips counts once" in report
    assert "| failed calls |" in report
    assert "| scenario | mode | checks |" in report
    assert "## f_g_strength" in report and "| | planner | legacy |" in report
    assert "### planner" in report and "### legacy" in report
    assert "Replace barbell bench press with dumbbell bench press" in report
    assert "Sat A: Easy jog (easy_jog), 1×25 min, bodyweight" in report
    assert "Mon A: BJJ class (activity:bjj_class), 1×90 min, bodyweight" in report
    assert "Mon A: Barbell back squat" in report
    assert "pass: any_of" in report and f"n/a: location — {LEGACY_NO_LOCATION}" in report
    assert report.rstrip().endswith(CLOSING_PARAGRAPH)


# --- The checks, on hand-made plans -------------------------------------------------------------


def _seen(plan: Plan | None, *, previous: Plan | None = None, **extra: Any) -> Observed:
    values: dict[str, Any] = {
        "mode": "planner",
        "reply": "",
        "staged": plan is not None,
        "plan": plan,
        "previous": previous,
        "has_draft": plan is not None,
        "saved_match": False,
        "profile_location": Location.PUBLIC_GYM,
        "history_kg": {"barbell_bench_press": 60.0},
    }
    values.update(extra)
    return Observed(**values)


def _outcomes(expect: dict[str, Any], seen: Observed) -> dict[str, str]:
    checks = evaluate(Expect.model_validate(expect), seen, load_catalog())
    return {check.name: check.outcome for check in checks}


def test_a_barbell_item_fails_the_equipment_and_kind_checks() -> None:
    plan = _run(location=Location.OUTDOOR)
    good = {"no_equipment": True, "exclude_kinds": ["compound"], "no_kg_loads": True}
    assert set(_outcomes(good, _seen(plan)).values()) == {"pass"}
    plan.workouts[0].blocks.append(_single(_BARBELL_BENCH))
    bad = _outcomes({**good, "exclude_equipment": ["barbell"]}, _seen(plan))
    assert bad == {
        "no_equipment": "fail",
        "exclude_kinds": "fail",
        "no_kg_loads": "fail",
        "exclude_equipment": "fail",
    }


def test_load_checks_read_the_plan_as_staged_or_fail_on_a_legacy_load_guard() -> None:
    staged = _strength(_reps("dumbbell_bench_press", Load(kind="kg", kg=60)))
    judged = _strength(_DUMBBELL_BENCH)  # the guards replaced the 60 kg with calibration
    swap = {"swap_load": {"from": "barbell_bench_press", "to": "dumbbell_bench_press"}}
    before = _strength(_BARBELL_BENCH)
    assert _outcomes(swap, _seen(judged, previous=before)) == {"swap_load": "pass"}
    assert _outcomes(swap, _seen(judged, previous=before, requested=staged)) == {
        "swap_load": "fail"
    }
    assert _outcomes({"no_kg_loads": True}, _seen(_bjj(), requested=staged)) == {
        "no_kg_loads": "fail"
    }
    no_history = _strength(_reps("barbell_bench_press", Load(kind="calibration")))
    assert _outcomes({"history_loads": True}, _seen(before)) == {"history_loads": "pass"}
    assert _outcomes({"history_loads": True}, _seen(before, requested=no_history)) == {
        "history_loads": "fail"
    }
    # Legacy: only the judged plan exists, so a load guard on the exercise fails the check.
    substituted = GuardVerdict(
        rule="loads.substituted",
        ok=True,
        detail="barbell_back_squat: proposed 200 kg rejected; load engine value 80 kg used",
    )
    legacy = _seen(before, mode="legacy", guards=[substituted])
    checks = evaluate(
        Expect.model_validate({"compound_loads": ["kg", "calibration"], "history_loads": True}),
        legacy,
        load_catalog(),
    )
    assert [(c.name, c.outcome) for c in checks] == [
        ("compound_loads", "fail"),
        ("history_loads", "pass"),  # the bench (the only history id here) had no guard
    ]
    assert "loads.substituted on barbell_back_squat" in checks[0].detail
    other = GuardVerdict(rule="plan.time_budget", ok=False, detail="barbell_back_squat: long")
    assert _outcomes(
        {"compound_loads": ["kg", "calibration"]}, _seen(before, mode="legacy", guards=[other])
    ) == {"compound_loads": "pass"}


def test_legacy_cannot_represent_a_location_or_an_activity() -> None:
    legacy = _seen(_run(location=None), mode="legacy")
    outcomes = _outcomes(
        {"location": "outdoor", "activity_items_min": 2, "outdoor_allowed": True}, legacy
    )
    assert outcomes == {"location": "n/a", "activity_items_min": "n/a", "outdoor_allowed": "pass"}
    # The profile's own location is representable, so it is still judged.
    assert _outcomes({"location": "public_gym"}, legacy) == {"location": "pass"}
    gym = _strength(_BARBELL_BENCH)
    assert _outcomes({"outdoor_allowed": True}, _seen(gym)) == {"outdoor_allowed": "fail"}


def test_single_change_counts_items_not_positions() -> None:
    before = _strength(_BARBELL_BENCH)
    one = _strength(_DUMBBELL_BENCH)
    expect = {
        "single_change": True,
        "unchanged_except": ["barbell_bench_press", "dumbbell_bench_press"],
    }
    assert _outcomes(expect, _seen(one, previous=before)) == {
        "single_change": "pass",
        "unchanged_except": "pass",
    }
    two = _strength(_DUMBBELL_BENCH)
    two.workouts[2].blocks[0].items[0] = _reps("bodyweight_squat", Load(kind="bodyweight"))
    assert _outcomes(expect, _seen(two, previous=before)) == {
        "single_change": "fail",
        "unchanged_except": "fail",
    }
    inserted = _strength(_BARBELL_BENCH)
    inserted.workouts[0].blocks.insert(0, _single(_timed("brisk_walk", 300)))
    assert _outcomes({"single_change": True}, _seen(inserted, previous=before)) == {
        "single_change": "pass"
    }
    assert _outcomes(expect, _seen(one)) == {"single_change": "n/a", "unchanged_except": "n/a"}


def test_a_swap_in_every_workout_is_one_change_and_leaves_no_barbell() -> None:
    def twice(bench: Prescription) -> Plan:
        plan = _strength(bench)
        plan.workouts[1].blocks.append(_single(bench.model_copy()))
        return plan

    before = twice(_BARBELL_BENCH)
    after = twice(_DUMBBELL_BENCH)
    expect = {
        "single_change": True,
        "exclude_ids": ["barbell_bench_press"],
        "unchanged_except": ["barbell_bench_press", "dumbbell_bench_press"],
    }
    assert set(_outcomes(expect, _seen(after, previous=before)).values()) == {"pass"}
    half = twice(_BARBELL_BENCH)
    half.workouts[0].blocks[1].items[0] = _DUMBBELL_BENCH
    outcomes = _outcomes(expect, _seen(half, previous=before))
    assert outcomes["exclude_ids"] == "fail"  # one barbell bench is left


def test_a_swap_may_not_carry_more_than_the_barbell_kg() -> None:
    before = _strength(_BARBELL_BENCH)
    expect = {"swap_load": {"from": "barbell_bench_press", "to": "dumbbell_bench_press"}}
    heavy = _strength(_reps("dumbbell_bench_press", Load(kind="kg", kg=70)))
    light = _strength(_reps("dumbbell_bench_press", Load(kind="kg", kg=22)))
    assert _outcomes(expect, _seen(heavy, previous=before)) == {"swap_load": "fail"}
    assert _outcomes(expect, _seen(light, previous=before)) == {"swap_load": "pass"}
    same = _strength(_reps("dumbbell_bench_press", Load(kind="kg", kg=60)))
    assert _outcomes(expect, _seen(same, previous=before)) == {"swap_load": "fail"}
    # With no barbell kg in the previous plan, the seeded history is the reference.
    assert _outcomes(expect, _seen(heavy)) == {"swap_load": "fail"}
    assert _outcomes(expect, _seen(before)) == {"swap_load": "fail"}  # no dumbbell item


def test_schedule_location_activity_and_reps_checks() -> None:
    plan = _bjj()
    expect = {
        "days": [0, 3],
        "days_count": 2,
        "workouts": 2,
        "items_max": 1,
        "activity_items_min": 2,
        "reps_items_max": 0,
        "location": "public_gym",
        "catalog_only": False,
    }
    assert set(_outcomes(expect, _seen(plan)).values()) == {"pass"}  # activity items
    assert _outcomes({"catalog_only": True}, _seen(plan)) == {"catalog_only": "fail"}
    assert _outcomes({"days": [1, 3], "location": "outdoor"}, _seen(plan)) == {
        "days": "fail",
        "location": "fail",
    }
    assert _outcomes({"compound_loads": ["calibration"]}, _seen(_strength(_DUMBBELL_BENCH))) == {
        "compound_loads": "fail"  # the squat is in kg
    }


def test_questions_drafts_saves_and_alternatives() -> None:
    asked = _seen(None, reply="Which days suit you?", staged=False)
    assert _outcomes({"question": True, "draft": False}, asked) == {
        "question": "pass",
        "draft": "pass",
    }
    assert _outcomes({"question": True}, _seen(None, reply="Done.")) == {"question": "fail"}
    # A "?" anywhere in the last non-empty line counts; one only on an earlier line does not.
    mid = _seen(None, reply="Walk, jog, walk.\nShall I put this into a plan? Or change it.\n\n")
    assert _outcomes({"question": True}, mid) == {"question": "pass"}
    early = _seen(None, reply="Shall I put this into a plan?\nThe jog is 25 min.")
    assert _outcomes({"question": True}, early) == {"question": "fail"}
    soft = evaluate(Expect.model_validate({"question": True}), asked, load_catalog())
    assert [c.soft for c in soft] == [True]
    staged = _seen(_bjj(), reply="Staged. Save it?")
    assert _outcomes({"question": True}, staged) == {"question": "fail"}
    assert _outcomes({"question": True}, _seen(None, mode="legacy")) == {"question": "n/a"}
    assert _outcomes({"saved": True}, _seen(_bjj(), saved_match=True)) == {"saved": "pass"}
    # A plan check with no plan fails.
    assert _outcomes({"days": [0]}, asked) == {"days": "fail"}

    either = {"any_of": [{"question": True}, {"draft": True, "activity_items_min": 2}]}
    checks = evaluate(Expect.model_validate(either), staged, load_catalog())
    assert [(c.name, c.outcome, c.soft) for c in checks] == [("any_of", "pass", True)]
    assert "alternative 2" in checks[0].detail
    neither = evaluate(Expect.model_validate(either), _seen(None, reply="Ok."), load_catalog())
    assert neither[0].outcome == "fail" and "no alternative passed" in neither[0].detail
    hard = {"any_of": [{"days": [0, 3]}, {"days": [1, 4]}]}
    checks = evaluate(Expect.model_validate(hard), staged, load_catalog())
    assert [(c.name, c.outcome, c.soft) for c in checks] == [("any_of", "pass", False)]


async def test_saved_counts_only_versions_written_in_that_turn(
    settings: Settings, tmp_path: Path
) -> None:
    """A seeded plan identical to the staged one does not make `saved` pass."""
    plan = _strength(_BARBELL_BENCH)
    fixture = ConversationFixture.model_validate(
        {
            "name": "seeded",
            "profile": "experienced_public_gym",
            "history": [{"exercise_id": "barbell_bench_press", "kg": 60, "reps": 6}],
            "plans": [plan.model_dump(mode="json")],
            "turns": [
                {"user": "the same again", "expect": {"draft": True, "saved": False}},
                {"user": "save it", "expect": {"saved": True}},
            ],
            "legacy": {"request": {"user": "the same again"}},
        }
    )
    script = Script(
        planner={
            "the same again": [_stage(plan), _say("Staged.")],
            "save it": [Call("save_draft"), _say("Saved.")],
        }
    )
    result = await run_conversation(
        fixture,
        mode="planner",
        llm=script.runtime(settings),
        profiles_dir=_FIXTURES.parent,
        work_dir=tmp_path,
    )
    assert result.error is None
    outcomes = [{c.name: c.outcome for c in turn.checks} for turn in result.turns]
    assert outcomes == [{"draft": "pass", "saved": "pass"}, {"saved": "pass"}]
    assert result.turns[1].plan is not None


async def test_failed_calls_are_counted_apart_and_keep_the_cost_known(tmp_path: Path) -> None:
    from fitme.db.controllers.decisions import insert_llm_call
    from fitme.db.controllers.users import insert_user
    from fitme.llm.eval_conversations import TurnRecord, _new_calls, _Run

    db = await open_database(tmp_path / "calls.db")
    try:
        await migrate(db)
        async with db.transaction() as conn:
            user_id = await insert_user(conn, language="en", timezone="UTC")
            for ok, tokens, cost in ((True, 100, 0.01), (False, 0, None)):
                await insert_llm_call(
                    conn,
                    decision_id=None,
                    purpose="planner",
                    model="m",
                    input_tokens=tokens,
                    output_tokens=tokens,
                    cost_estimate_usd=cost,
                    latency_ms=1,
                    ok=ok,
                )
        run = _Run(
            db=db,
            llm=None,  # type: ignore[arg-type]  # not used for accounting
            user_id=user_id,
            lang="en",
            mode="planner",
            profile_location=Location.PUBLIC_GYM,
            history_kg={},
            catalog=load_catalog(),
        )
        turn = TurnRecord(index=0, action="message", user="x")
        await _new_calls(run, turn)
    finally:
        await db.close()
    assert (turn.model_calls, turn.failed_calls, turn.input_tokens) == (1, 1, 100)
    assert turn.cost_known and turn.cost_usd == pytest.approx(0.01)
    assert turn.purposes == ("planner", "planner")


def test_a_model_override_resolves_every_agent_and_tier(settings: Settings) -> None:
    overridden = with_model_override(settings, "anthropic:claude-haiku-4-5")
    for tier in TIERS:
        assert resolve_tier(tier, overridden) == "anthropic:claude-haiku-4-5"
    for agent in AGENT_DEFAULT_TIER:
        assert model_for(agent, overridden).model == "anthropic:claude-haiku-4-5"
    assert (
        model_for("planner", settings).model != model_for("planner", overridden).model
    )  # the original is kept


# --- The CLI ------------------------------------------------------------------------------------


async def test_the_cli_refuses_without_yes(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await llm_eval_conversations.run(
        settings, mode="both", scenario=None, model=None, confirmed=False
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "spends real money" in out and "Refusing to run without --yes." in out
