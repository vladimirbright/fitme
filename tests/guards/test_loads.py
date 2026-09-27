"""`services.loads.next_load`, the pure double-progression core (A§7.3; A§7.4: "no history
-> calibration; all sets hit reps_max -> +increment; unknown check-in -> hold; the weekly cap
already used -> hold; two sessions below reps_min -> -10% rounded; a ceiling violation is
rejected"; B2: the decrease fallback is `calibration`, never the catalog kg start, and never
above the prescribed load; B3: progression is computed from the *prescribed* load, not what
was actually logged)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fitme.db.records import SessionOutcome
from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer
from fitme.domain.models import Load
from fitme.services.loads import ExerciseHistory, next_load


def _squat(**overrides: object) -> Exercise:
    defaults: dict[str, object] = {
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
    defaults.update(overrides)
    return Exercise.model_validate(defaults)


_FINE_CHECKINS = {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE}
# Every loaded area flagged: the strict case, where each area needs a `fine` check-in.
_ALL_AREAS = frozenset({"knee", "lower_back"})


def _outcome(
    *,
    load_kg: float | None,
    planned_load_kg: float | None = None,
    hit_reps_max: bool,
    below_reps_min: bool,
) -> SessionOutcome:
    """`planned_load_kg` defaults to `load_kg` (the ordinary case: the user logged exactly
    what was prescribed); tests exercising B3 pass a different value explicitly."""
    return SessionOutcome(
        load_kg=load_kg,
        planned_load_kg=load_kg if planned_load_kg is None else planned_load_kg,
        hit_reps_max=hit_reps_max,
        below_reps_min=below_reps_min,
    )


def test_no_history_is_calibration() -> None:
    exercise = _squat()
    decision = next_load(
        exercise,
        ExerciseHistory(history_max_kg=None, sessions=[]),
        {},
        [],
        cap_kg=2.5,
        flagged_areas=_ALL_AREAS,
    )
    assert decision.load == Load(kind="calibration")  # never the catalog kg start (A§7.3)
    assert decision.load != exercise.start
    assert "calibration" in decision.reason


def test_unflagged_area_needs_no_checkin_for_an_increment() -> None:
    """A§6.5/A§7: check-ins are asked for flagged areas only, so with nothing flagged an
    increment passes the check-in guard with no check-ins at all (cap and ceiling still
    apply)."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    decision = next_load(exercise, history, {}, increases_7d=[], cap_kg=2.5, flagged_areas=())
    assert decision.load.kg == 102.5
    held = next_load(
        exercise, history, {}, increases_7d=[], cap_kg=2.5, flagged_areas={"lower_back"}
    )
    assert held.load.kg == 100.0  # the flagged area has no check-in: blocked


def test_hit_reps_max_proposes_an_increment_when_every_guard_passes() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kind == "kg"
    assert decision.load.kg == 102.5
    assert all(v.ok for v in decision.guards_fired)


def test_unknown_checkin_holds_instead_of_incrementing() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    checkins = {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.FINE}
    decision = next_load(
        exercise, history, checkins, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 100.0  # held, not incremented
    assert any(not v.ok for v in decision.guards_fired)


def test_weekly_cap_already_used_holds_instead_of_incrementing() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[2.5], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 100.0
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in decision.guards_fired)


def test_ceiling_violation_holds_instead_of_incrementing() -> None:
    exercise = _squat(load_step_kg=2.5)
    # history_max_kg lower than the last session's load: an unusual but possible state (e.g.
    # a manually-edited plan, or history deleted per A§9.4); the proposed increment (102.5)
    # would breach the 97.5 kg ceiling, so no increment. The hold itself (100) is above the
    # ceiling too, so it is clamped down to 97.5 (A§7.3: every emitted kg passes the ceiling).
    history = ExerciseHistory(
        history_max_kg=95.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 97.5
    assert any(v.rule == "ceiling.historical_max" and not v.ok for v in decision.guards_fired)


def test_two_sessions_below_reps_min_decreases_by_10_percent_rounded() -> None:
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[
            _outcome(load_kg=100.0, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=100.0, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    # 100 * 0.9 = 90.0, already a multiple of the 2.5 kg step.
    assert decision.load.kg == 90.0
    assert "10%" in decision.reason


def test_two_sessions_below_reps_min_rounds_to_the_implement_step() -> None:
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=42.5,
        sessions=[
            _outcome(load_kg=42.5, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=42.5, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    # 42.5 * 0.9 = 38.25 -> nearest 2.5 kg step is 37.5.
    assert decision.load.kg == 37.5


def test_a_single_session_below_reps_min_holds_without_decreasing() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=False, below_reps_min=True)],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 100.0
    assert "hold" in decision.reason


def test_two_sessions_below_reps_min_floors_rather_than_rounds() -> None:
    """The decrease floors to the step (never rounds up), so it's never accidentally a
    smaller cut than intended. 13 kg * 0.9 = 11.7; round-to-nearest would give 12.5 kg (only
    -0.5 kg); flooring gives 10.0 kg (a full 2.5 kg step down)."""
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=13.0,
        sessions=[
            _outcome(load_kg=13.0, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=13.0, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 10.0


def test_two_sessions_below_reps_min_from_0_5_kg_falls_back_to_calibration() -> None:
    """B2: 0.5 kg * 0.9 = 0.45, which floors to 0 kg at a 1 kg step. The fallback is an
    explicit `Load(kind="calibration")`, never the catalog's kg `start` (which is unguarded
    and could sit *above* the current load, silently re-escalating instead of decreasing)."""
    exercise = _squat(load_step_kg=1.0)
    history = ExerciseHistory(
        history_max_kg=0.5,
        sessions=[
            _outcome(load_kg=0.5, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=0.5, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kind == "calibration"
    assert decision.load != exercise.start  # not the (unguarded) catalog kg start
    assert "calibration" in decision.reason


def test_two_sessions_below_reps_min_from_2_0_kg_step_1_decreases_by_one_step() -> None:
    """2.0 kg * 0.9 = 1.8, which floors to 1.0 kg at a 1 kg step — exactly one full step
    down, not a no-op."""
    exercise = _squat(load_step_kg=1.0)
    history = ExerciseHistory(
        history_max_kg=2.0,
        sessions=[
            _outcome(load_kg=2.0, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=2.0, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 1.0


def test_decrease_never_returns_a_kg_load_above_the_prescribed_load() -> None:
    """B2 invariant, checked across a spread of starting loads and steps: whenever the
    decrease path returns a `kg` load at all, it must be strictly below the prescribed load
    that triggered it (never a no-op, never an increase from rounding)."""
    for prescribed_kg, step in (
        (100.0, 2.5),
        (42.5, 2.5),
        (13.0, 2.5),
        (2.0, 1.0),
        (20.0, 1.0),
        (5.0, 0.5),
        (3.0, 2.5),
    ):
        exercise = _squat(load_step_kg=step)
        history = ExerciseHistory(
            history_max_kg=prescribed_kg,
            sessions=[
                _outcome(load_kg=prescribed_kg, hit_reps_max=False, below_reps_min=True),
                _outcome(load_kg=prescribed_kg, hit_reps_max=False, below_reps_min=True),
            ],
        )
        decision = next_load(
            exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
        )
        if decision.load.kind == "kg":
            assert decision.load.kg is not None
            assert 0 < decision.load.kg < prescribed_kg, (prescribed_kg, step, decision.load)
        else:
            assert decision.load.kind == "calibration"


def test_hit_reps_max_progresses_from_the_prescribed_load_not_the_logged_one() -> None:
    """B3: prescribed 60 kg, but the user logged (and hit reps_max at) 70 kg — the next
    proposal must be 60 + 2.5 = 62.5, never 70 + 2.5 = 72.5. Self-escalating by logging a
    heavier weight than prescribed must not jump the next prescription."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=70.0,
        sessions=[
            _outcome(load_kg=70.0, planned_load_kg=60.0, hit_reps_max=True, below_reps_min=False)
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 62.5


def test_hold_shows_the_prescribed_load_not_the_logged_one() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=70.0,
        sessions=[
            _outcome(load_kg=70.0, planned_load_kg=60.0, hit_reps_max=False, below_reps_min=False)
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 60.0


def test_steady_state_between_reps_min_and_reps_max_holds() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=False, below_reps_min=False)],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load.kg == 100.0
    # Every emitted kg value is ceiling-checked, and the verdict is logged (AGENTS.md §6).
    assert [v.rule for v in decision.guards_fired] == ["ceiling.historical_max"]
    assert all(v.ok for v in decision.guards_fired)


# --- A§7.3: after a calibration session, anchor on what was logged — never the catalog kg
# start (reviewer probe: squat start 20, user logged 12 in calibration -> 20 kg emitted with
# no guards, above the 14.5 kg ceiling). ---


def test_after_calibration_session_anchors_on_the_logged_load_not_the_catalog_start() -> None:
    exercise = _squat()  # catalog start 20 kg
    history = ExerciseHistory(
        history_max_kg=12.0,
        sessions=[
            SessionOutcome(
                load_kg=12.0, planned_load_kg=None, hit_reps_max=False, below_reps_min=False
            )
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=12.0)
    assert decision.load != exercise.start
    assert any(v.rule == "ceiling.historical_max" and v.ok for v in decision.guards_fired)


def test_after_calibration_session_all_sets_hit_still_holds_at_the_logged_load() -> None:
    """There is no prescribed load to progress from after calibration (A§7.3: progression
    is computed from the prescribed load), so the next session is the logged load, held."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=12.0,
        sessions=[
            SessionOutcome(
                load_kg=12.0, planned_load_kg=None, hit_reps_max=True, below_reps_min=False
            )
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=12.0)


def test_after_calibration_session_logged_above_the_catalog_start_anchors_on_the_log() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=30.0,
        sessions=[
            SessionOutcome(
                load_kg=30.0, planned_load_kg=None, hit_reps_max=False, below_reps_min=False
            )
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=30.0)


def test_after_calibration_session_with_nothing_logged_is_calibration_again() -> None:
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=None,
        sessions=[
            SessionOutcome(
                load_kg=None, planned_load_kg=None, hit_reps_max=False, below_reps_min=False
            )
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="calibration")
    assert decision.load != exercise.start


# --- A§7.3: every kg value the engine emits, on every path, passes `check_ceiling`; a hold
# above the ceiling is clamped down (floored to the step) or falls back to calibration.
# (Reviewer probe: prescribed 60, logged 50, history max 50 -> the hold returned 60, above
# the 52.5 kg ceiling.) ---


def test_hold_above_the_ceiling_is_clamped_down_to_the_ceiling() -> None:
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=50.0,
        sessions=[
            _outcome(load_kg=50.0, planned_load_kg=60.0, hit_reps_max=False, below_reps_min=False)
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=52.5)
    assert "clamped" in decision.reason
    assert any(v.rule == "ceiling.historical_max" and not v.ok for v in decision.guards_fired)


def test_hold_above_the_ceiling_is_floored_to_the_implement_step() -> None:
    """History max 50, increment 2.5 -> ceiling 52.5; at a 5 kg step that floors to 50."""
    exercise = _squat(load_step_kg=5.0)
    history = ExerciseHistory(
        history_max_kg=50.0,
        sessions=[
            _outcome(load_kg=50.0, planned_load_kg=60.0, hit_reps_max=False, below_reps_min=False)
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=50.0)


def test_held_increase_above_the_ceiling_is_clamped_too() -> None:
    """The increase path's hold (a guard failed) goes through the same ceiling check:
    prescribed 60 hit reps_max but the unknown check-in blocks the increase, and 60 itself is
    above the 52.5 kg ceiling."""
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=50.0,
        sessions=[
            _outcome(load_kg=50.0, planned_load_kg=60.0, hit_reps_max=True, below_reps_min=False)
        ],
    )
    checkins = {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.FINE}
    decision = next_load(
        exercise, history, checkins, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=52.5)


def test_decrease_above_the_ceiling_is_clamped_too() -> None:
    """Prescribed 100 twice below reps_min -> -10% = 90, but the user only ever logged 50:
    the 52.5 kg ceiling wins."""
    exercise = _squat(load_step_kg=2.5)
    history = ExerciseHistory(
        history_max_kg=50.0,
        sessions=[
            _outcome(load_kg=50.0, planned_load_kg=100.0, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=50.0, planned_load_kg=100.0, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="kg", kg=52.5)


def test_hold_with_no_history_max_falls_back_to_calibration() -> None:
    """A prescribed load but no logged kg at all (e.g. history deleted, A§9.4): there is no
    ceiling to clamp to, so the only safe kg-free answer is calibration."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=None,
        sessions=[
            _outcome(load_kg=None, planned_load_kg=60.0, hit_reps_max=False, below_reps_min=False)
        ],
    )
    decision = next_load(
        exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=_ALL_AREAS
    )
    assert decision.load == Load(kind="calibration")


_SWEEP_INCREMENT = 2.5
_SWEEP_KG_VALUES = (0.5, 2.0, 5.0, 12.0, 20.0, 42.5, 50.0, 60.0, 61.0, 100.0)
_SWEEP_OUTCOMES = ((True, False), (False, True), (False, False))
_SWEEP_CHECKINS = (
    _FINE_CHECKINS,
    {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.FINE},
)


@dataclass(frozen=True, slots=True)
class _SweepCase:
    exercise: Exercise
    history: ExerciseHistory
    checkins: dict[str, CheckinAnswer]
    history_max: float
    logged: float
    prescribed: float | None


def _sweep_cases() -> Iterator[_SweepCase]:
    """Every (step, history max, logged, prescribed incl. calibration, outcome, previous
    outcome, check-ins) combination the property tests below run the engine over."""
    for step in (1.0, 2.5, 5.0):
        exercise = _squat(load_step_kg=step, increment_kg=_SWEEP_INCREMENT)
        for history_max in _SWEEP_KG_VALUES:
            for logged in _SWEEP_KG_VALUES:
                for prescribed in (*_SWEEP_KG_VALUES, None):
                    for hit, below in _SWEEP_OUTCOMES:
                        for prev_below in (False, True):
                            for checkins in _SWEEP_CHECKINS:
                                last = SessionOutcome(
                                    load_kg=logged,
                                    planned_load_kg=prescribed,
                                    hit_reps_max=hit,
                                    below_reps_min=below,
                                )
                                previous = SessionOutcome(
                                    load_kg=logged,
                                    planned_load_kg=prescribed,
                                    hit_reps_max=False,
                                    below_reps_min=prev_below,
                                )
                                yield _SweepCase(
                                    exercise=exercise,
                                    history=ExerciseHistory(
                                        history_max_kg=history_max, sessions=[last, previous]
                                    ),
                                    checkins=checkins,
                                    history_max=history_max,
                                    logged=logged,
                                    prescribed=prescribed,
                                )


def test_every_emitted_kg_value_is_within_the_ceiling() -> None:
    """Property-style sweep over many (history max, prescribed, logged, outcome) combinations,
    including calibration sessions and two-session histories: whatever path the engine
    takes, an emitted kg value is never above history_max + increment_kg."""
    checked = 0
    for case in _sweep_cases():
        decision = next_load(
            case.exercise,
            case.history,
            case.checkins,
            increases_7d=[],
            cap_kg=2.5,
            flagged_areas=_ALL_AREAS,
        )
        checked += 1
        if decision.load.kind != "kg":
            assert decision.load.kind == "calibration"
            continue
        assert decision.load.kg is not None
        assert decision.load.kg <= case.history_max + _SWEEP_INCREMENT + 1e-9, (case, decision)
    assert checked > 1000


def test_with_the_weekly_cap_already_used_no_emitted_kg_exceeds_the_reference() -> None:
    """Same sweep with the weekly cap already spent (`increases_7d=[cap]`): no path may
    raise the load, so every emitted kg is at most the prescribed load (or the logged load
    after a calibration session) — and still within the ceiling."""
    checked = 0
    for case in _sweep_cases():
        decision = next_load(
            case.exercise,
            case.history,
            case.checkins,
            increases_7d=[2.5],
            cap_kg=2.5,
            flagged_areas=_ALL_AREAS,
        )
        checked += 1
        if decision.load.kind != "kg":
            assert decision.load.kind == "calibration"
            continue
        reference = case.logged if case.prescribed is None else case.prescribed
        assert decision.load.kg is not None
        assert decision.load.kg <= reference + 1e-9, (case, decision)
        assert decision.load.kg <= case.history_max + _SWEEP_INCREMENT + 1e-9, (case, decision)
    assert checked > 1000


def test_engine_never_emits_kg_for_a_non_kg_loadable_exercise() -> None:
    """A§4.4: bodyweight for a bodyweight move, calibration otherwise — even with history."""
    pushup = _squat(
        id="pushup",
        kind="bodyweight",
        pattern="horizontal_push",
        equipment=[],
        loads_areas=["shoulder"],
        start={"kind": "bodyweight"},
    )
    rower = _squat(
        id="machine_rower",
        kind="cardio",
        pattern="conditioning",
        equipment=["machine"],
        loads_areas=["lower_back"],
        start={"kind": "calibration"},
    )
    history = ExerciseHistory(
        history_max_kg=100.0,
        sessions=[_outcome(load_kg=100.0, hit_reps_max=True, below_reps_min=False)],
    )
    assert not pushup.kg_loadable and not rower.kg_loadable
    for exercise, expected in ((pushup, "bodyweight"), (rower, "calibration")):
        decision = next_load(
            exercise, history, _FINE_CHECKINS, increases_7d=[], cap_kg=2.5, flagged_areas=()
        )
        assert decision.load.kind == expected, exercise.id


def test_engine_holds_at_the_load_applied_this_week() -> None:
    """A§7: the last session (40, hit reps_max) already earned the +2.5 that a confirmed
    plan applied this week (42.5, cap used). The engine holds at 42.5 instead of re-deriving
    the increase from the session and having the cap knock it back to 40."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=40.0,
        sessions=[_outcome(load_kg=40.0, hit_reps_max=True, below_reps_min=False)],
    )
    decision = next_load(
        exercise,
        history,
        _FINE_CHECKINS,
        increases_7d=[2.5],
        cap_kg=2.5,
        flagged_areas=_ALL_AREAS,
        applied_to_kg_7d=42.5,
    )
    assert decision.load == Load(kind="kg", kg=42.5)
    # An applied load below the prescription changes nothing; a garbage one fails closed.
    lower = next_load(
        exercise,
        history,
        _FINE_CHECKINS,
        increases_7d=[2.5],
        cap_kg=2.5,
        flagged_areas=_ALL_AREAS,
        applied_to_kg_7d=30.0,
    )
    assert lower.load.kg == 40.0
    for bad in (float("nan"), float("inf"), 0.0):
        garbage = next_load(
            exercise,
            history,
            _FINE_CHECKINS,
            increases_7d=[],
            cap_kg=2.5,
            flagged_areas=_ALL_AREAS,
            applied_to_kg_7d=bad,
        )
        assert garbage.load.kind == "calibration", bad


def test_sweep_with_an_applied_load_this_week_stays_within_the_ceiling() -> None:
    """The cap-used sweep again, with a load applied this week one increment above the
    prescription: every emitted kg is at most that applied load (never a fresh increase on
    top of it) and always within the ceiling."""
    checked = 0
    for case in _sweep_cases():
        applied = None if case.prescribed is None else case.prescribed + _SWEEP_INCREMENT
        decision = next_load(
            case.exercise,
            case.history,
            case.checkins,
            increases_7d=[2.5],
            cap_kg=2.5,
            flagged_areas=_ALL_AREAS,
            applied_to_kg_7d=applied,
        )
        checked += 1
        if decision.load.kind != "kg":
            assert decision.load.kind == "calibration"
            continue
        reference = case.logged if case.prescribed is None else case.prescribed
        bound = reference if applied is None else max(reference, applied)
        assert decision.load.kg is not None
        assert decision.load.kg <= bound + 1e-9, (case, decision)
        assert decision.load.kg <= case.history_max + _SWEEP_INCREMENT + 1e-9, (case, decision)
    assert checked > 1000


def test_two_sessions_below_reps_min_decrease_even_with_a_load_applied_this_week() -> None:
    """A§7: the engine's decrease rule beats holding at an applied load. Two completed
    sessions under `reps_min` at 42.5 never come back as "hold at 45 applied this week"."""
    exercise = _squat()
    history = ExerciseHistory(
        history_max_kg=45.0,
        sessions=[
            _outcome(load_kg=42.5, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=42.5, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decision = next_load(
        exercise,
        history,
        _FINE_CHECKINS,
        increases_7d=[2.5],
        cap_kg=2.5,
        flagged_areas=_ALL_AREAS,
        applied_to_kg_7d=45.0,
    )
    assert decision.load == Load(kind="kg", kg=37.5)
    assert "two sessions below reps_min" in decision.reason
    # A single failed session still holds at the applied load (no decrease yet).
    single = ExerciseHistory(
        history_max_kg=45.0,
        sessions=[
            _outcome(load_kg=42.5, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=42.5, hit_reps_max=True, below_reps_min=False),
        ],
    )
    held = next_load(
        exercise,
        single,
        _FINE_CHECKINS,
        increases_7d=[2.5],
        cap_kg=2.5,
        flagged_areas=_ALL_AREAS,
        applied_to_kg_7d=45.0,
    )
    assert held.load == Load(kind="kg", kg=45.0)


def test_sweep_after_two_failed_sessions_the_engine_never_restores_an_applied_load() -> None:
    """The applied-load sweep restricted to histories with two sessions under `reps_min`:
    whatever load was applied this week, every emitted kg is strictly below the prescribed
    load (a decrease) or calibration, and within the ceiling."""
    checked = 0
    for case in _sweep_cases():
        if case.prescribed is None or len(case.history.sessions) < 2:
            continue
        if not (
            case.history.sessions[0].below_reps_min and case.history.sessions[1].below_reps_min
        ):
            continue
        applied = case.prescribed + _SWEEP_INCREMENT
        decision = next_load(
            case.exercise,
            case.history,
            case.checkins,
            increases_7d=[2.5],
            cap_kg=2.5,
            flagged_areas=_ALL_AREAS,
            applied_to_kg_7d=applied,
        )
        checked += 1
        if decision.load.kind != "kg":
            assert decision.load.kind == "calibration"
            continue
        assert decision.load.kg is not None
        assert decision.load.kg < case.prescribed, (case, decision)
        assert decision.load.kg <= case.history_max + _SWEEP_INCREMENT + 1e-9, (case, decision)
    assert checked > 100


def test_decision_kind_is_structured() -> None:
    """M8: the recap reads `LoadDecision.kind`/`blocked_by`, never the `reason` text."""
    from fitme.services.loads import BlockedBy, LoadKind

    exercise = _squat()
    hit = ExerciseHistory(
        history_max_kg=40.0,
        sessions=[_outcome(load_kg=40.0, hit_reps_max=True, below_reps_min=False)],
    )
    increase = next_load(exercise, hit, _FINE_CHECKINS, [], 2.5, flagged_areas=_ALL_AREAS)
    assert increase.kind == LoadKind.INCREASE and increase.blocked_by is None
    capped = next_load(exercise, hit, _FINE_CHECKINS, [2.5], 2.5, flagged_areas=_ALL_AREAS)
    assert (capped.kind, capped.blocked_by) == (LoadKind.HOLD_BLOCKED, BlockedBy.WEEKLY_CAP)
    unknown = next_load(exercise, hit, {}, [], 2.5, flagged_areas=_ALL_AREAS)
    assert (unknown.kind, unknown.blocked_by) == (LoadKind.HOLD_BLOCKED, BlockedBy.CHECKIN)
    # A hold above the ceiling (prescribed 60, only 50 ever logged) is clamped.
    stale = ExerciseHistory(
        history_max_kg=50.0,
        sessions=[_outcome(load_kg=60.0, hit_reps_max=False, below_reps_min=False)],
    )
    clamped = next_load(exercise, stale, _FINE_CHECKINS, [], 2.5, flagged_areas=_ALL_AREAS)
    assert (clamped.kind, clamped.blocked_by) == (LoadKind.CLAMPED, BlockedBy.CEILING)
    assert clamped.load.kg == 52.5
    none = ExerciseHistory(history_max_kg=None)
    assert next_load(exercise, none, {}, [], 2.5, flagged_areas=()).kind == LoadKind.CALIBRATION
    failed_twice = ExerciseHistory(
        history_max_kg=40.0,
        sessions=[
            _outcome(load_kg=40.0, hit_reps_max=False, below_reps_min=True),
            _outcome(load_kg=40.0, hit_reps_max=False, below_reps_min=True),
        ],
    )
    decrease = next_load(exercise, failed_twice, _FINE_CHECKINS, [], 2.5, flagged_areas=_ALL_AREAS)
    assert decrease.kind == LoadKind.DECREASE
