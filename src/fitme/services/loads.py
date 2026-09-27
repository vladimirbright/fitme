"""The deterministic load engine (A§7.3): produces the explicit load shown in workout review
and recap. Deterministic double progression, built on the guards in `fitme.guards`.

`next_load` is the pure core: no I/O, takes plain domain/dataclass values, and always
returns a decision plus the guard verdicts it consulted (for `decisions.guards_fired`,
AGENTS.md §6). `next_load_for_exercise` is the thin async wrapper that reads history from the
DB (A§4.6 unit-of-work rules: a single short `db.read()`, no network call inside it) and
calls the pure core.

Invariant (A§7.3): **every kg value the engine emits, on every path, passes `check_ceiling`**
(AGENTS.md §2 "ceiling on absolute load"). The increase path checks the proposal up front;
every hold and decrease goes through `_within_ceiling`, which clamps a value above the
ceiling down to it (floored to the implement step) or falls back to a `calibration` load.
The catalog's kg `start` is only ever emitted when there is no history at all.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from fitme.db.connection import Database
from fitme.db.records import SessionOutcome
from fitme.db.selectors.decisions import applied_to_kg_by_exercise, recent_increase_deltas
from fitme.db.selectors.training import historical_max_kg, recent_session_outcomes
from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load
from fitme.guards.ceiling import check_ceiling
from fitme.guards.checkins import increase_allowed
from fitme.guards.progression import check_weekly_increment

_DECREASE_FACTOR = 0.9  # A§7.3: propose -10% after two sessions below reps_min


@dataclass(frozen=True, slots=True)
class ExerciseHistory:
    """What `next_load` needs to know about an exercise's past sessions.

    `sessions` is most-recent-first; an empty sequence means "no history at all" (A§7.3: the
    first session is calibration, not progression). `history_max_kg` is independent of
    `sessions` because it's an all-time max (A§9.4: deleting recent logs can only lower it,
    never raise it back up from older ones that remain).
    """

    history_max_kg: float | None
    sessions: Sequence[SessionOutcome] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LoadDecision:
    """The engine's output: the load to show, why, and every guard verdict consulted while
    deciding (logged verbatim to `decisions.guards_fired`, AGENTS.md §6)."""

    load: Load
    reason: str
    guards_fired: list[GuardVerdict] = field(default_factory=list)


def _is_valid_kg(value: float) -> bool:
    """A usable working weight: finite and positive (A§7: load inputs must be finite and
    positive; `Load(kind="kg")` itself rejects anything else)."""
    return math.isfinite(value) and value > 0


def _round_down_to_step(value: float, step: float) -> float:
    """Floor `value` to the nearest multiple of `step`. Rounding a *decrease* down (never up)
    means the result is never accidentally higher than what was actually earned; the outer
    `round(..., 6)` only clears float noise (e.g. `19.999999999998`), it doesn't change which
    multiple of `step` was chosen."""
    if step <= 0:
        return value
    return round(math.floor(value / step) * step, 6)


def _calibration(reason: str, guards_fired: list[GuardVerdict]) -> LoadDecision:
    """`Load(kind="calibration")` means "log what you actually use", which is always safe.
    It is the fallback whenever a kg value can't be justified — never the catalog kg start,
    which is unguarded and could sit above what the user can currently do."""
    return LoadDecision(load=Load(kind="calibration"), reason=reason, guards_fired=guards_fired)


def _within_ceiling(
    kg: float,
    reason: str,
    guards_fired: list[GuardVerdict],
    *,
    exercise: Exercise,
    history: ExerciseHistory,
) -> LoadDecision:
    """Emit `kg` only if it passes `check_ceiling`; otherwise clamp it down to the ceiling
    (floored to the implement step) or fall back to calibration. Every hold and decrease
    goes through here, so the ceiling holds on every path, not just the increase one."""
    verdict = check_ceiling(
        history_max_kg=history.history_max_kg,
        proposed_load_kg=kg,
        increment_kg=exercise.increment_kg,
    )
    guards_fired = [*guards_fired, verdict]
    if verdict.ok:
        return LoadDecision(load=Load(kind="kg", kg=kg), reason=reason, guards_fired=guards_fired)

    fallback_reason = f"{reason}; {verdict.detail}; falling back to calibration — log what you use"
    history_max_kg = history.history_max_kg
    if history_max_kg is None or not _is_valid_kg(history_max_kg):
        return _calibration(fallback_reason, guards_fired)
    ceiling_kg = history_max_kg + exercise.increment_kg
    clamped_kg = _round_down_to_step(ceiling_kg, exercise.load_step_kg or 1.0)
    if not (0 < clamped_kg <= ceiling_kg):
        return _calibration(fallback_reason, guards_fired)
    return LoadDecision(
        load=Load(kind="kg", kg=clamped_kg),
        reason=f"{reason}; clamped to the ceiling: {verdict.detail}",
        guards_fired=guards_fired,
    )


def next_load(
    exercise: Exercise,
    history: ExerciseHistory,
    checkins: Mapping[str, CheckinAnswer],
    increases_7d: Sequence[float],
    cap_kg: float,
    *,
    flagged_areas: Collection[str],
    applied_to_kg_7d: float | None = None,
) -> LoadDecision:
    """Deterministic double progression (A§7.3). `flagged_areas` is the set of loads-area
    names the user flagged (`guards.checkins.flagged_areas_from`): only those need a `fine`
    check-in before an increase. `applied_to_kg_7d` is the highest `to_kg` already applied
    to this exercise in the trailing 7 days (`GuardContext.applied_to_kg_7d`), if any: a
    plan confirmed at that load is the current prescription, so the engine holds there
    rather than re-deriving (and re-capping) the same increase from the last session.

    - no history -> `calibration` (never a kg value; the catalog `start` is a display hint);
    - the last session was itself a calibration session (no prescribed load) -> anchor on
      what the user logged, held (there is no prescribed load to progress from); if nothing
      usable was logged, calibration again. Never the catalog kg start;
    - the last session hit `reps_max` -> propose `planned_load_kg + increment_kg` (the
      *prescribed* load, not what was actually logged — B3: self-escalating by logging a
      heavier weight than prescribed must not jump the next prescription; it only raises the
      historical max the ceiling guard uses), gated by `checkins.increase_allowed`,
      `progression.check_weekly_increment` and `ceiling.check_ceiling`; any one failing keeps
      the current (prescribed) load — a hold;
    - the last session fell below `reps_min`, and so did the one before it -> propose -10% of
      the prescribed load, floored to `exercise.load_step_kg`, always at least one step down,
      never above the prescribed load (decreases are always allowed, no guards to fail);
    - a single session below `reps_min`, or anything else -> hold at the prescribed load.

    Every hold and decrease is then passed through `_within_ceiling`: a kg value above the
    ceiling (e.g. prescribed 60, but the user only ever logged 50) is clamped down or
    replaced by calibration.
    """
    if not exercise.kg_loadable:
        # A§4.4 "non-kg exercises": bodyweight for a bodyweight move, calibration for the rest
        # (a cardio machine, a band). Never a kg number, whatever the history says.
        if exercise.start.kind == "bodyweight":
            return LoadDecision(load=Load(kind="bodyweight"), reason="not kg-loadable: bodyweight")
        return _calibration("not kg-loadable: calibration", [])
    if not history.sessions:
        return _calibration("no history: calibration", [])

    last = history.sessions[0]
    prescribed_kg = last.planned_load_kg

    if applied_to_kg_7d is not None and not _is_valid_kg(applied_to_kg_7d):
        return _calibration(
            f"applied_to_kg_7d {applied_to_kg_7d!r} is not a finite positive number; failing "
            "closed to calibration — log what you use",
            [],
        )
    if (
        prescribed_kg is not None
        and applied_to_kg_7d is not None
        and applied_to_kg_7d > prescribed_kg
    ):
        # A§7: the increase was already applied (and guarded) this week; hold at it.
        return _within_ceiling(
            applied_to_kg_7d,
            "hold at the load applied this week",
            [],
            exercise=exercise,
            history=history,
        )

    if prescribed_kg is None:
        # After a calibration session there is nothing to progress from: anchor on what the
        # user actually logged (A§7.3), never on the catalog kg start.
        logged_kg = last.load_kg
        if logged_kg is None or not _is_valid_kg(logged_kg):
            return _calibration("after calibration: nothing usable logged; calibration again", [])
        return _within_ceiling(
            logged_kg,
            "after calibration: hold at the logged load",
            [],
            exercise=exercise,
            history=history,
        )

    if last.hit_reps_max:
        proposed_kg = prescribed_kg + exercise.increment_kg
        checkins_verdict = increase_allowed(exercise, checkins, flagged_areas)
        weekly_verdict = check_weekly_increment(
            exercise,
            increases_7d=increases_7d,
            proposed_increase_kg=exercise.increment_kg,
            cap_kg=cap_kg,
        )
        ceiling_verdict = check_ceiling(
            history_max_kg=history.history_max_kg,
            proposed_load_kg=proposed_kg,
            increment_kg=exercise.increment_kg,
        )
        fired = [checkins_verdict, weekly_verdict, ceiling_verdict]
        if checkins_verdict.ok and weekly_verdict.ok and ceiling_verdict.ok:
            return LoadDecision(
                load=Load(kind="kg", kg=proposed_kg),
                reason="hit reps_max; increment applied",
                guards_fired=fired,
            )
        first_failure = next(verdict for verdict in fired if not verdict.ok)
        return _within_ceiling(
            prescribed_kg,
            f"hit reps_max, but held: {first_failure.detail}",
            fired,
            exercise=exercise,
            history=history,
        )

    if last.below_reps_min:
        two_in_a_row = len(history.sessions) >= 2 and history.sessions[1].below_reps_min
        if two_in_a_row:
            step = exercise.load_step_kg or 1.0
            decreased_kg = _round_down_to_step(prescribed_kg * _DECREASE_FACTOR, step)
            # Always at least one full step down from the prescribed load, never a no-op or
            # an accidental increase from rounding.
            if decreased_kg >= prescribed_kg:
                decreased_kg = _round_down_to_step(prescribed_kg - step, step)
            # B2: never return a kg value at or above the prescribed load, and never fall
            # back to the catalog's kg `start`.
            if not (0 < decreased_kg < prescribed_kg):
                return _calibration(
                    "two sessions below reps_min: -10% would reach <= 0 (or not decrease "
                    "at all); falling back to calibration — log what you use",
                    [],
                )
            return _within_ceiling(
                decreased_kg,
                "two sessions below reps_min: -10%, floored to the implement step",
                [],
                exercise=exercise,
                history=history,
            )
        return _within_ceiling(
            prescribed_kg, "below reps_min: hold", [], exercise=exercise, history=history
        )

    return _within_ceiling(
        prescribed_kg, "hold: steady state", [], exercise=exercise, history=history
    )


async def next_load_for_exercise(
    db: Database,
    *,
    user_id: int,
    exercise: Exercise,
    checkins: Mapping[str, CheckinAnswer],
    flagged_areas: Collection[str],
    cap_kg: float,
    since_7d: str,
) -> LoadDecision:
    """Read this exercise's history in one `db.read()`, then decide purely.

    `since_7d` is the caller-computed trailing-7-day cutoff (`clock.format_timestamp`), kept
    a plain parameter rather than computed here, so this stays testable without freezing the
    clock (`fitme.clock` is the only place that produces "now").
    """
    async with db.read() as conn:
        history_max = await historical_max_kg(conn, user_id, exercise.id)
        sessions = await recent_session_outcomes(conn, user_id, exercise.id, limit=2)
        increases_7d = await recent_increase_deltas(conn, user_id, exercise.id, since=since_7d)
        applied = await applied_to_kg_by_exercise(conn, user_id, since=since_7d)
    history = ExerciseHistory(history_max_kg=history_max, sessions=sessions)
    return next_load(
        exercise,
        history,
        checkins,
        increases_7d,
        cap_kg,
        flagged_areas=flagged_areas,
        applied_to_kg_7d=applied.get(exercise.id),
    )
