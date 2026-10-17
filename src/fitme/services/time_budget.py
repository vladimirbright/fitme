"""ADR 0016: a newly generated workout that doesn't fit the user's session length is
shortened by priority, deterministically — the model is not asked to do the arithmetic.

A workout is over budget when its estimate (`services.session_time`) exceeds the session
length by more than the estimate's tolerance. Then, until it fits:

1. **redundant accessories first**: an accessory or core block that trains none of the
   workout's stated objectives goes;
2. **then volume**: one set off each remaining trimmable block (never below two sets),
   lowest priority first;
3. **then whole blocks**: the lowest-priority trimmable block goes, one at a time.

What is never touched: the warm-up, any block that directly trains the workout's main
objective (its first `qualities` entry; without stated objectives, the power and strength
blocks), the last working block (in a mobility-only session, the last mobility block), and
every rest period — rest on heavy or fast work is what
keeps it heavy or fast, so a workout is made shorter, never denser.

Priority, lowest first: accessory, core, cool-down mobility, conditioning, then other power
and strength blocks; within that, a block that trains none of the workout's stated
objectives before one that does, and later blocks before earlier ones. A stated secondary
objective whose last exercise is removed is dropped from `Workout.qualities`, so the workout
never claims what it no longer trains.

Only `planning.propose_new_plan` uses this: a revision, an import or the user's own edit is
what the user asked for, and the review only reports its length.
"""

from __future__ import annotations

from dataclasses import dataclass

from fitme.domain.catalog import Catalog
from fitme.domain.enums import BlockRole, Quality
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, Plan, Workout
from fitme.services import session_time
from fitme.services.plan_quality import block_roles

RULE = "plan.time_budget"
_MIN_SETS = 2
_RANK: dict[BlockRole | None, int] = {
    None: 0,
    BlockRole.ACCESSORY: 0,
    BlockRole.CORE: 1,
    BlockRole.MOBILITY: 2,
    BlockRole.CONDITIONING: 3,
    BlockRole.POWER: 4,
    BlockRole.STRENGTH: 5,
}


@dataclass(slots=True)
class _Candidate:
    block: Block
    role: BlockRole | None
    index: int
    serves_objective: bool


def _qualities(block: Block, catalog: Catalog) -> set[Quality]:
    return {
        quality
        for item in block.items
        if (exercise := catalog.by_id(item.exercise_id)) is not None
        for quality in exercise.qualities
    }


def _protected(block: Block, role: BlockRole | None, workout: Workout, catalog: Catalog) -> bool:
    if role == BlockRole.WARM_UP:
        return True
    if workout.qualities:
        return workout.qualities[0] in _qualities(block, catalog)
    return role in (BlockRole.POWER, BlockRole.STRENGTH)


def _candidates(workout: Workout, catalog: Catalog) -> list[_Candidate]:
    roles = block_roles(workout, catalog)
    stated = set(workout.qualities)
    found = [
        _Candidate(
            block=block,
            role=role,
            index=index,
            serves_objective=bool(stated & _qualities(block, catalog)),
        )
        for index, (block, role) in enumerate(zip(workout.blocks, roles, strict=True))
        if not _protected(block, role, workout, catalog)
    ]
    return sorted(
        found,
        key=lambda c: (_RANK.get(c.role, 0), c.serves_objective, -c.index),
    )


def _names(block: Block) -> str:
    return " + ".join(item.exercise_id for item in block.items)


def fit_workout(
    workout: Workout,
    catalog: Catalog,
    session_minutes: int,
    model: session_time.TimeModel = session_time.DEFAULT_MODEL,
) -> list[GuardVerdict]:
    """Shorten `workout` in place (see the module docstring). Returns one informational
    verdict describing what changed, or nothing when it already fits."""
    budget = session_minutes * model.tolerance
    before = session_time.estimate(workout, catalog, model).minutes
    if before <= budget:
        return []

    def fits() -> bool:
        return session_time.estimate(workout, catalog, model).minutes <= budget

    changes: list[str] = []

    def last_working(block: Block) -> bool:
        roles = block_roles(workout, catalog)
        working = [
            other
            for other, role in zip(workout.blocks, roles, strict=True)
            if role not in (BlockRole.WARM_UP, BlockRole.MOBILITY)
        ]
        if not working:  # a mobility-only session: its mobility blocks are the work
            working = [
                other
                for other, role in zip(workout.blocks, roles, strict=True)
                if role != BlockRole.WARM_UP
            ]
        return any(other is block for other in working) and len(working) <= 1

    def remove(block: Block) -> bool:
        if last_working(block):
            return False  # never the last working block: a workout is never emptied
        workout.blocks = [other for other in workout.blocks if other is not block]
        changes.append(f"removed {_names(block)}")
        return True

    if workout.qualities:
        for candidate in _candidates(workout, catalog):
            if fits():
                break
            redundant = candidate.role in (None, BlockRole.ACCESSORY, BlockRole.CORE)
            if redundant and not candidate.serves_objective:
                remove(candidate.block)

    for candidate in _candidates(workout, catalog):
        if fits():
            break
        block = candidate.block
        if max(item.sets for item in block.items) <= _MIN_SETS or last_working(block):
            continue  # the only working block keeps its volume too
        old = max(item.sets for item in block.items)
        for item in block.items:
            if item.sets > _MIN_SETS:
                item.sets -= 1
        changes.append(f"{_names(block)} {old} → {old - 1} sets")

    for candidate in _candidates(workout, catalog):
        if fits():
            break
        remove(candidate.block)

    if not changes:
        return []
    remaining = set().union(*(_qualities(block, catalog) for block in workout.blocks))
    dropped = [quality for quality in workout.qualities[1:] if quality not in remaining]
    if dropped:
        workout.qualities = [quality for quality in workout.qualities if quality not in dropped]
        changes.append("secondary objective dropped: " + ", ".join(q.value for q in dropped))
    after = session_time.estimate(workout, catalog, model).minutes
    return [
        GuardVerdict(
            rule=RULE,
            ok=True,
            detail=(
                f"workout {workout.key}: about {before:.0f} min for a {session_minutes} min "
                f"session; {'; '.join(changes)}; now about {after:.0f} min"
            ),
        )
    ]


def fit_plan(
    plan: Plan,
    catalog: Catalog,
    session_minutes: int,
    model: session_time.TimeModel = session_time.DEFAULT_MODEL,
) -> list[GuardVerdict]:
    """`fit_workout` for every workout of `plan`, in place."""
    return [
        verdict
        for workout in plan.workouts
        for verdict in fit_workout(workout, catalog, session_minutes, model)
    ]
