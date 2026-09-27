"""`guards.checkins.increase_allowed` (A§7, A§7.4: "an unknown check-in blocks an increase";
A§6.5: check-ins exist for flagged areas only, so only a *flagged* loaded area needs a `fine`
answer; ALSO REQUIRED: empty `loads_areas` fails closed)."""

from __future__ import annotations

from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer, ScreeningFlag
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.checkins import flagged_areas_from, increase_allowed

_BOTH = frozenset({"knee", "lower_back"})


def _squat() -> Exercise:
    return Exercise.model_validate(
        {
            "id": "barbell_back_squat",
            "names": {"en": "Barbell back squat"},
            "kind": "compound",
            "pattern": "squat",
            "equipment": ["barbell", "rack"],
            "locations": ["public_gym"],
            "loads_areas": ["knee", "lower_back"],
            "increment_kg": 2.5,
            "start": {"kind": "kg", "kg": 20.0},
            "instructions": {"en": "..."},
        }
    )


def test_unknown_checkin_on_a_flagged_area_blocks_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.FINE}, _BOTH
    )
    assert verdict.ok is False


def test_flagged_area_with_no_checkin_at_all_is_blocked() -> None:
    verdict = increase_allowed(_squat(), {"lower_back": CheckinAnswer.FINE}, _BOTH)
    assert verdict.ok is False
    assert increase_allowed(_squat(), {}, {"knee"}).ok is False


def test_flagged_area_whose_latest_checkin_is_unknown_after_an_older_fine_is_blocked() -> None:
    """The mapping holds the *latest* answer per area (`selectors.training.latest_checkin_
    answers`): a newer unanswered check-in supersedes an older `fine` one."""
    latest = {"knee": CheckinAnswer.UNKNOWN}  # older "fine" already superseded
    assert increase_allowed(_squat(), latest, {"knee"}).ok is False


def test_worse_or_pain_checkin_on_a_flagged_area_blocks_an_increase() -> None:
    for answer in (CheckinAnswer.WORSE, CheckinAnswer.PAIN):
        verdict = increase_allowed(
            _squat(), {"knee": answer, "lower_back": CheckinAnswer.FINE}, _BOTH
        )
        assert verdict.ok is False, answer


def test_flagged_areas_with_the_latest_checkin_fine_allow_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE}, _BOTH
    )
    assert verdict.ok is True
    assert increase_allowed(_squat(), {"knee": CheckinAnswer.FINE}, {"knee"}).ok is True


def test_unflagged_areas_need_no_checkin() -> None:
    """Nothing flagged: no check-in was ever asked (A§6.5), so none is required — the cap
    and ceiling guards still gate the increase."""
    assert increase_allowed(_squat(), {}, frozenset()).ok is True
    # An unflagged area's stale answer is irrelevant too: only flagged areas are checked.
    assert increase_allowed(_squat(), {"lower_back": CheckinAnswer.WORSE}, {"knee"}).ok is False
    assert (
        increase_allowed(
            _squat(), {"lower_back": CheckinAnswer.WORSE, "knee": CheckinAnswer.FINE}, {"knee"}
        ).ok
        is True
    )


def test_an_exercise_with_no_loaded_areas_fails_closed() -> None:
    """An exercise reaching this guard with empty `loads_areas` has nothing to check an
    increase against; that must block, not silently allow (fail closed) — flagged or not."""
    exercise = Exercise.model_validate(
        {
            "id": "plank",
            "names": {"en": "Plank"},
            "kind": "bodyweight",
            "pattern": "core",
            "equipment": [],
            "locations": ["public_gym"],
            "increment_kg": 1.0,
            "start": {"kind": "bodyweight"},
            "instructions": {"en": "..."},
        }
    )
    assert increase_allowed(exercise, {}, frozenset()).ok is False
    assert increase_allowed(exercise, {}, {"knee"}).ok is False


def test_flagged_areas_from_maps_yes_area_flags_to_loads_areas() -> None:
    flags = [
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes"),
        ScreeningFlagState(flag=ScreeningFlag.LOWER_BACK_INJURY_CURRENT, value="no"),
        ScreeningFlagState(flag=ScreeningFlag.HERNIA, value="yes"),  # no loads area
        ScreeningFlagState(flag=ScreeningFlag.HEART_CONDITION, value="yes"),  # not an area
    ]
    assert flagged_areas_from(flags) == frozenset({"knee"})
    assert flagged_areas_from([]) == frozenset()
