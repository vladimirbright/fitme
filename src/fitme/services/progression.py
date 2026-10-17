"""ADR 0016: progression for the work the load engine (`services.loads`, A§7.3) does not
progress — bodyweight exercises in reps, and anything prescribed in seconds or meters (a
plank, an interval, a carry). Pure and deterministic; the recap offers its result as an
ordinary suggestion the user applies with a tap, so nothing changes in a plan by itself.

It reads **actual** performance (`set_logs`) against the **planned** target the user had at
the time, and only the sessions done at the current target and number of sets count: after a
change to either, the evidence starts again. Imported history never counts (A§7.3). Per exercise:

- **speed work** (`IntensityMode.SPEED`: throws, jumps, sprints, footwork) is never
  progressed by volume — more reps or longer bouts aren't faster ones. It is held, and the
  recap says it progresses by speed and clean technique; the app measures neither, so it
  claims neither;
- **loaded** time or distance work (a kg-loadable exercise such as a carry) counts a
  session only at the same logged resistance as the newer one: 12 kg then 24 kg per hand
  over 40 m is a load progression, not two sessions at 40 m; a session without a logged
  load is no evidence. Bodyweight work (a plank, jump rope) has no resistance to compare;
- **two sessions in a row** with every prescribed set performed at the top of the target
  (`reps_max`, the prescribed seconds or meters) -> **one** step up: the rep range by 1-2,
  or about 10% more time or distance (at least 5 s / 5 m). One variable at a time: never the
  sets as well (ADR 0015's set-count suggestions stay the model's, and are dropped for an
  exercise that gets one of these);
- at the top of a useful range (`Caps`) -> no more of the same: a harder variation is the
  next step, which only the user (or a swap suggestion) can choose;
- **two sessions in a row** with every prescribed set logged and one of them short of the
  target (below `reps_min`, under the seconds or meters) -> one step down;
- anything else, including a single session either way, a skipped set or missing results ->
  hold. One session is not a trend, and a partial one is no evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from fitme.db.records import SetLogRecord
from fitme.domain.catalog import Exercise, intensity_mode
from fitme.domain.enums import IntensityMode, Measure
from fitme.domain.models import Prescription
from fitme.domain.results import ChangeReps, ChangeTarget

SESSIONS_FOR_A_TREND = 2


class TargetKind(StrEnum):
    MORE = "more"
    LESS = "less"
    HOLD = "hold"
    SPEED = "speed"  # held: fast work progresses by speed and technique
    VARIATION = "variation"  # at the top of a useful range: a harder variation is next
    NOT_APPLICABLE = "not_applicable"  # the load engine progresses it


@dataclass(frozen=True, slots=True)
class Caps:
    """The top of a useful range, beyond which more of the same isn't the progression."""

    reps_max: int = 20
    hold_seconds: int = 120  # a plank, a loaded hold, a carry by time
    interval_seconds: int = 300  # one conditioning interval
    carry_meters: int = 100
    run_meters: int = 800


CAPS = Caps()


@dataclass(frozen=True, slots=True)
class TargetDecision:
    kind: TargetKind
    reason: str
    change: ChangeReps | ChangeTarget | None = None


def applies(item: Prescription, exercise: Exercise) -> bool:
    """Whether this prescription is progressed here rather than by the load engine: not a
    kg-loadable exercise in reps."""
    return not (exercise.kg_loadable and item.measure == Measure.REPS)


def _target(item: Prescription) -> tuple[int | None, int | None, int | None, int | None]:
    return (item.reps_min, item.reps_max, item.seconds, item.meters)


def _planned(row: SetLogRecord) -> tuple[int | None, int | None, int | None, int | None]:
    return (row.planned_reps_min, row.planned_reps_max, row.planned_seconds, row.planned_meters)


def _value(row: SetLogRecord, measure: Measure) -> int | None:
    if measure == Measure.TIME:
        return row.actual_seconds
    if measure == Measure.DISTANCE:
        return row.actual_meters
    return row.actual_reps


def _top_and_floor(item: Prescription) -> tuple[int, int]:
    """(the value that meets the target, the value below which a set is short)."""
    if item.measure == Measure.TIME:
        assert item.seconds is not None
        return item.seconds, item.seconds
    if item.measure == Measure.DISTANCE:
        assert item.meters is not None
        return item.meters, item.meters
    assert item.reps_min is not None and item.reps_max is not None
    return item.reps_max, item.reps_min


def session_result(item: Prescription, rows: Sequence[SetLogRecord]) -> str | None:
    """One past session against the current target: `met` (every prescribed set performed
    at the top), `short` (a performed set below the floor), `mixed`, or `None` when that
    session was done at a different target (a change since, so it isn't evidence)."""
    prescribed = [row for row in rows if not row.extra]
    # Another target or another number of sets is another prescription: not evidence.
    if (
        not prescribed
        or len(prescribed) != item.sets
        or any(_planned(row) != _target(item) for row in prescribed)
    ):
        return None
    top, floor = _top_and_floor(item)
    values = [_value(row, item.measure) for row in prescribed]
    # A skipped or unlogged set makes the session inconclusive either way: it neither earns
    # more nor, with a short set next to it, counts as falling short.
    if any(row.skipped or value is None for row, value in zip(prescribed, values, strict=True)):
        return "mixed"
    if any(value is not None and value < floor for value in values):
        return "short"
    if all(value is not None and value >= top for value in values):
        return "met"
    return "mixed"


def session_resistance(item: Prescription, rows: Sequence[SetLogRecord]) -> float | None:
    """The one external load every prescribed set of a loaded exercise was done with, as
    logged (per implement for two dumbbells, as the plan states it) — or `None` when a set
    has no logged load, the sets differ, or the session was prescribed another kg load than
    the current one. Never assumed: a missing load is no evidence."""
    prescribed = [row for row in rows if not row.extra]
    if item.load.kind == "kg" and any(row.planned_load_kg != item.load.kg for row in prescribed):
        return None
    loads = {row.actual_load_kg for row in prescribed}
    if len(loads) != 1:
        return None
    load = next(iter(loads))
    return None if load is None or load <= 0 else load


def _step(value: int) -> int:
    """About 10%, in steps of 5, at least 5."""
    return max(5, round(value * 0.1 / 5) * 5)


def _cap(item: Prescription, exercise: Exercise, caps: Caps) -> int:
    mode = intensity_mode(exercise, item.measure)
    if item.measure == Measure.TIME:
        return caps.interval_seconds if mode == IntensityMode.PACE else caps.hold_seconds
    if item.measure == Measure.DISTANCE:
        return caps.run_meters if mode == IntensityMode.PACE else caps.carry_meters
    return caps.reps_max


def _more(
    item: Prescription, exercise: Exercise, caps: Caps
) -> tuple[ChangeReps | ChangeTarget | None, str]:
    cap = _cap(item, exercise, caps)
    if item.measure == Measure.REPS:
        assert item.reps_min is not None and item.reps_max is not None
        if item.reps_max >= cap:
            return None, f"already at {item.reps_max} reps"
        step = 2 if item.reps_max >= 8 else 1
        high = min(cap, item.reps_max + step)
        low = min(high, item.reps_min + (high - item.reps_max))
        return ChangeReps(exercise_id=exercise.id, reps_min=low, reps_max=high), (
            f"{item.reps_min}–{item.reps_max} -> {low}–{high} reps"
        )
    if item.measure == Measure.TIME:
        assert item.seconds is not None
        if item.seconds >= cap:
            return None, f"already at {item.seconds} s"
        seconds = min(cap, item.seconds + _step(item.seconds))
        return ChangeTarget(exercise_id=exercise.id, from_target=item.seconds, seconds=seconds), (
            f"{item.seconds} -> {seconds} s"
        )
    assert item.meters is not None
    if item.meters >= cap:
        return None, f"already at {item.meters} m"
    meters = min(cap, item.meters + _step(item.meters))
    change = ChangeTarget(exercise_id=exercise.id, from_target=item.meters, meters=meters)
    return change, f"{item.meters} -> {meters} m"


def _less(item: Prescription, exercise: Exercise) -> tuple[ChangeReps | ChangeTarget | None, str]:
    if item.measure == Measure.REPS:
        assert item.reps_min is not None and item.reps_max is not None
        step = 2 if item.reps_min > 4 else 1
        low = max(1, item.reps_min - step)
        high = max(low, item.reps_max - step)
        if (low, high) == (item.reps_min, item.reps_max):
            return None, "already at the lowest range"
        return ChangeReps(exercise_id=exercise.id, reps_min=low, reps_max=high), (
            f"{item.reps_min}–{item.reps_max} -> {low}–{high} reps"
        )
    if item.measure == Measure.TIME:
        assert item.seconds is not None
        seconds = max(5, item.seconds - _step(item.seconds))
        if seconds == item.seconds:
            return None, "already at the shortest target"
        return ChangeTarget(exercise_id=exercise.id, from_target=item.seconds, seconds=seconds), (
            f"{item.seconds} -> {seconds} s"
        )
    assert item.meters is not None
    meters = max(5, item.meters - _step(item.meters))
    if meters == item.meters:
        return None, "already at the shortest target"
    change = ChangeTarget(exercise_id=exercise.id, from_target=item.meters, meters=meters)
    return change, f"{item.meters} -> {meters} m"


def next_target(
    item: Prescription,
    exercise: Exercise,
    sessions: Sequence[Sequence[SetLogRecord]],
    caps: Caps = CAPS,
) -> TargetDecision:
    """The next target for `item` from this exercise's completed sessions (most recent
    first, `selectors.training.recent_completed_set_logs`). See the module docstring."""
    if not applies(item, exercise):
        return TargetDecision(TargetKind.NOT_APPLICABLE, "load engine")
    if intensity_mode(exercise, item.measure) == IntensityMode.SPEED:
        return TargetDecision(
            TargetKind.SPEED, "speed work: volume held; progress by speed and clean technique"
        )
    results: list[str] = []
    resistance: float | None = None
    why = "not a trend yet"
    for rows in sessions[:SESSIONS_FOR_A_TREND]:
        result = session_result(item, rows)
        if result is None:
            break  # done at another target: the evidence for this one starts after it
        if exercise.kg_loadable:
            # Loaded time or distance work (a carry): the evidence is the target *at one
            # resistance*. A heavier or lighter session, or one whose load wasn't logged, is
            # another progression variable having changed — the streak starts after it.
            load = session_resistance(item, rows)
            if load is None:
                why = "a session without one logged load is no evidence"
                break
            if resistance is not None and load != resistance:
                why = (
                    f"the load changed ({load:g} -> {resistance:g} kg): evidence starts at "
                    "the new one"
                )
                break
            resistance = load
        results.append(result)
    if len(results) < SESSIONS_FOR_A_TREND:
        return TargetDecision(TargetKind.HOLD, f"{len(results)} session(s) at this target: {why}")
    if all(result == "met" for result in results):
        change, detail = _more(item, exercise, caps)
        if change is None:
            return TargetDecision(
                TargetKind.VARIATION, f"target met twice, {detail}: a harder variation is next"
            )
        return TargetDecision(TargetKind.MORE, f"target met twice in a row: {detail}", change)
    if all(result == "short" for result in results):
        change, detail = _less(item, exercise)
        if change is None:
            return TargetDecision(TargetKind.HOLD, f"short twice, {detail}")
        reason = f"short of the target twice in a row: {detail}"
        return TargetDecision(TargetKind.LESS, reason, change)
    return TargetDecision(TargetKind.HOLD, "mixed results: hold")
