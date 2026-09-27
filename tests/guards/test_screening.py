"""`guards.screening` (A§7, AGENTS.md §2 "Screening before first plan"; A§7.4: "a red flag
without clearance leads to refusal"; "an open hold leads to refusal"; "a contraindicated
exercise is rejected"; B4: "silence is not consent" for red flags)."""

from __future__ import annotations

from fitme.domain.catalog import Exercise
from fitme.domain.enums import RED_FLAGS, HealthHoldReason, ScreeningFlag
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.screening import exercise_allowed, plan_allowed


def _squat(contraindicated_by: list[str]) -> Exercise:
    return Exercise.model_validate(
        {
            "id": "barbell_back_squat",
            "names": {"en": "Barbell back squat"},
            "kind": "compound",
            "pattern": "squat",
            "equipment": ["barbell", "rack"],
            "locations": ["public_gym"],
            "loads_areas": ["knee", "lower_back"],
            "contraindicated_by": contraindicated_by,
            "increment_kg": 2.5,
            "start": {"kind": "kg", "kg": 20.0},
            "instructions": {"en": "..."},
        }
    )


def _all_red_flags_answered_no(**overrides: ScreeningFlagState) -> list[ScreeningFlagState]:
    """A "complete" screening: every red flag explicitly answered "no". Tests that exercise
    something other than the completeness check (B4) start from this baseline and override
    individual flags via `overrides` (keyed by the flag's enum member)."""
    states = {flag: ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS}
    for flag, state in overrides.items():
        states[flag] = state
    return list(states.values())


def test_red_flag_without_clearance_refuses_the_plan() -> None:
    flags = _all_red_flags_answered_no(
        heart_condition=ScreeningFlagState(
            flag=ScreeningFlag.HEART_CONDITION, value="yes", clearance=None
        )
    )
    verdict = plan_allowed(flags, holds=[])
    assert verdict.ok is False


def test_red_flag_with_clearance_allows_the_plan() -> None:
    flags = _all_red_flags_answered_no(
        heart_condition=ScreeningFlagState(
            flag=ScreeningFlag.HEART_CONDITION, value="yes", clearance="yes"
        )
    )
    verdict = plan_allowed(flags, holds=[])
    assert verdict.ok is True


def test_other_unlisted_without_clearance_refuses_like_a_red_flag() -> None:
    flags = _all_red_flags_answered_no() + [
        ScreeningFlagState(flag=ScreeningFlag.OTHER_UNLISTED, value="yes", clearance="no")
    ]
    verdict = plan_allowed(flags, holds=[])
    assert verdict.ok is False


def test_every_red_flag_answered_no_and_no_holds_allows_the_plan() -> None:
    assert plan_allowed(_all_red_flags_answered_no(), holds=[]).ok is True


def test_no_flags_at_all_refuses_the_plan() -> None:
    """B4: silence is not consent. With no screening answers at all, every red flag is
    missing, which must refuse — not silently proceed as if everything were "no"."""
    verdict = plan_allowed([], holds=[])
    assert verdict.ok is False
    assert verdict.rule == "screening.incomplete"


def test_one_missing_red_flag_refuses_the_plan() -> None:
    """Every *other* red flag answered "no", but one left out entirely."""
    states = _all_red_flags_answered_no()
    flags = [state for state in states if state.flag != ScreeningFlag.PREGNANT]
    verdict = plan_allowed(flags, holds=[])
    assert verdict.ok is False
    assert verdict.rule == "screening.incomplete"


def test_an_unknown_red_flag_answer_refuses_the_plan() -> None:
    flags = _all_red_flags_answered_no(
        heart_condition=ScreeningFlagState(flag=ScreeningFlag.HEART_CONDITION, value="unknown")
    )
    verdict = plan_allowed(flags, holds=[])
    assert verdict.ok is False
    assert verdict.rule == "screening.incomplete"


def test_open_health_hold_refuses_the_plan_even_without_flags() -> None:
    verdict = plan_allowed([], holds=[HealthHoldReason.STOP_WORD])
    assert verdict.ok is False


def test_an_area_flag_answered_no_does_not_need_clearance() -> None:
    flags = _all_red_flags_answered_no() + [
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="no")
    ]
    assert plan_allowed(flags, holds=[]).ok is True


def test_an_unanswered_area_flag_does_not_block_the_plan() -> None:
    """B4's completeness requirement is scoped to `RED_FLAGS`; an unanswered *area* flag
    (e.g. a fresh profile that hasn't reached the injury checklist yet) isn't a red flag and
    must not, on its own, refuse the plan."""
    assert plan_allowed(_all_red_flags_answered_no(), holds=[]).ok is True


def test_contraindicated_exercise_is_rejected() -> None:
    exercise = _squat(contraindicated_by=["knee_injury_current"])
    flags = [ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes")]
    verdict = exercise_allowed(exercise, flags)
    assert verdict.ok is False


def test_contraindication_applies_regardless_of_clearance() -> None:
    """Clearance is about permission to train at all (a red-flag condition), not about a
    specific injury area; a "yes" injury flag still contraindicates even if cleared."""
    exercise = _squat(contraindicated_by=["knee_injury_current"])
    flags = [
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes", clearance="yes")
    ]
    assert exercise_allowed(exercise, flags).ok is False


def test_non_contraindicated_exercise_is_allowed() -> None:
    exercise = _squat(contraindicated_by=["knee_injury_current"])
    flags = [ScreeningFlagState(flag=ScreeningFlag.SHOULDER_INJURY_CURRENT, value="yes")]
    assert exercise_allowed(exercise, flags).ok is True


def test_a_flag_answered_no_does_not_contraindicate() -> None:
    exercise = _squat(contraindicated_by=["knee_injury_current"])
    flags = [ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="no")]
    assert exercise_allowed(exercise, flags).ok is True
