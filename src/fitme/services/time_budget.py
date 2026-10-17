"""ADR 0016: a newly generated workout that doesn't fit the user's session length is
shortened by priority, deterministically — the model is not asked to do the arithmetic.

**The target is the session length itself.** The estimate's tolerance
(`session_time.TimeModel.tolerance`) is uncertainty, not extra time: a workout estimated
over `session_minutes` is shortened toward it, and the review only *warns*
(`quality.duration`) when what is left is still over by more than the tolerance.

Every block gets its relevance to the workout's objectives (`plan_quality.relevance`, the
same definition the review uses: primary, secondary, supporting, none) and a role rank
(accessory, core, cool-down mobility, conditioning, power, strength). Lower relevance goes
first, then the lower rank, then the later block. Until the estimate fits, recomputed after
every change:

1. **redundant accessories**: an accessory or core block that serves none of the stated
   objectives is removed, lowest priority first;
2. **volume, one set at a time**: the lowest-priority block that still has more than two
   sets loses one (among equals, the one with the most sets), so 6 sets can become 5, 4, 3
   before anything else is removed;
3. **whole blocks**: only when every trimmable block is down to two sets, the
   lowest-priority one is removed.

Never touched: the warm-up, any block that directly trains the main objective (without
stated objectives, the power and strength blocks), the last working block, and every rest
period — rest on heavy or fast work is what keeps it heavy or fast, so a workout is made
shorter, never denser. A stated secondary objective nothing trains any more is dropped from
`Workout.qualities`. When the target can't be reached without that protected work, the
verdict says so (`ok=False`) and the workout is left with its protected work intact.

The loop terminates: each step removes a set or a block, and both are finite.

Only `planning.propose_new_plan` uses this, and it re-validates the result: a revision, an
import or the user's own edit is what the user asked for, and the review only reports its
length.
"""

from __future__ import annotations

from fitme.domain.catalog import Catalog
from fitme.domain.enums import BlockRole, Quality
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, Plan, Workout
from fitme.services import session_time
from fitme.services.plan_quality import Relevance, block_relevance, block_roles

RULE = "plan.time_budget"
MIN_SETS = 2
_RANK: dict[BlockRole | None, int] = {
    None: 0,
    BlockRole.ACCESSORY: 0,
    BlockRole.CORE: 1,
    BlockRole.MOBILITY: 2,
    BlockRole.CONDITIONING: 3,
    BlockRole.POWER: 4,
    BlockRole.STRENGTH: 5,
}
_REDUNDANT_ROLES = (None, BlockRole.ACCESSORY, BlockRole.CORE)


def _qualities(block: Block, catalog: Catalog) -> set[Quality]:
    return {
        quality
        for item in block.items
        if (exercise := catalog.by_id(item.exercise_id)) is not None
        for quality in exercise.qualities
    }


def _names(block: Block) -> str:
    return " + ".join(item.exercise_id for item in block.items)


def _sets(block: Block) -> int:
    return max(item.sets for item in block.items)


class _Fitter:
    """One workout's reduction; recomputes roles and relevance on the current blocks."""

    def __init__(self, workout: Workout, catalog: Catalog, model: session_time.TimeModel):
        self.workout = workout
        self.catalog = catalog
        self.model = model
        # In order of first change: ("removed", block) or ("sets", block), and each reduced
        # block's original set count, so "6 → 5", "5 → 4" reads "6 → 4 sets".
        self.changes: list[tuple[str, Block]] = []
        self.original_sets: dict[int, int] = {}

    def minutes(self) -> float:
        return session_time.estimate(self.workout, self.catalog, self.model).minutes

    def _roles(self) -> list[BlockRole | None]:
        return block_roles(self.workout, self.catalog)

    def _working(self) -> list[Block]:
        pairs = list(zip(self.workout.blocks, self._roles(), strict=True))
        working = [b for b, r in pairs if r not in (BlockRole.WARM_UP, BlockRole.MOBILITY)]
        # A mobility-only session: its mobility blocks are the work.
        return working or [b for b, r in pairs if r != BlockRole.WARM_UP]

    def _protected(self, block: Block, role: BlockRole | None) -> bool:
        if role == BlockRole.WARM_UP:
            return True
        working = self._working()
        if len(working) <= 1 and any(block is other for other in working):
            return True  # never the last working block: a workout is never emptied
        if self.workout.qualities:
            relevance = block_relevance(block, self.catalog, self.workout.qualities)
            return relevance == Relevance.PRIMARY
        return role in (BlockRole.POWER, BlockRole.STRENGTH)

    def _trimmable(self) -> list[tuple[tuple[int, int, int, int], Block, BlockRole | None]]:
        """Unprotected blocks with their priority key, lowest priority first."""
        ranked = []
        for index, (block, role) in enumerate(zip(self.workout.blocks, self._roles(), strict=True)):
            if self._protected(block, role):
                continue
            relevance = block_relevance(block, self.catalog, self.workout.qualities)
            key = (int(relevance), _RANK.get(role, 0), -_sets(block), -index)
            ranked.append((key, block, role))
        return sorted(ranked, key=lambda entry: entry[0])

    def _remove(self, block: Block) -> None:
        self.workout.blocks = [other for other in self.workout.blocks if other is not block]
        self.changes.append(("removed", block))

    def fit(self, target: float) -> bool:
        if self.workout.qualities:
            while self.minutes() > target:
                redundant = [
                    block
                    for _key, block, role in self._trimmable()
                    if role in _REDUNDANT_ROLES
                    and block_relevance(block, self.catalog, self.workout.qualities)
                    == Relevance.NONE
                ]
                if not redundant:
                    break
                self._remove(redundant[0])
        while self.minutes() > target:
            reducible = [
                block for _key, block, _role in self._trimmable() if _sets(block) > MIN_SETS
            ]
            if not reducible:
                break
            block = reducible[0]
            if id(block) not in self.original_sets:
                self.original_sets[id(block)] = _sets(block)
                self.changes.append(("sets", block))
            for item in block.items:
                if item.sets > MIN_SETS:
                    item.sets -= 1
        while self.minutes() > target:
            trimmable = self._trimmable()
            if not trimmable:
                break
            self._remove(trimmable[0][1])
        return self.minutes() <= target

    def described(self) -> list[str]:
        return [
            f"removed {_names(block)}"
            if kind == "removed"
            else f"{_names(block)} {self.original_sets[id(block)]} → {_sets(block)} sets"
            for kind, block in self.changes
        ]


def fit_workout(
    workout: Workout,
    catalog: Catalog,
    session_minutes: int,
    model: session_time.TimeModel = session_time.DEFAULT_MODEL,
) -> list[GuardVerdict]:
    """Shorten `workout` in place toward `session_minutes` (see the module docstring).
    Returns one verdict describing what changed — `ok=False` when the target can't be
    reached without the protected work — or nothing when it already fits."""
    fitter = _Fitter(workout, catalog, model)
    before = fitter.minutes()
    if before <= session_minutes:
        return []
    fitted = fitter.fit(float(session_minutes))
    changes = fitter.described()
    remaining = set().union(*(_qualities(block, catalog) for block in workout.blocks))
    dropped = [quality for quality in workout.qualities[1:] if quality not in remaining]
    if dropped:
        workout.qualities = [quality for quality in workout.qualities if quality not in dropped]
        changes.append("secondary objective dropped: " + ", ".join(q.value for q in dropped))
    after = fitter.minutes()
    outcome = (
        f"now about {after:.0f} min"
        if fitted
        else f"still about {after:.0f} min: the rest is the warm-up and the main objective's "
        "work, which are kept"
    )
    return [
        GuardVerdict(
            rule=RULE,
            ok=fitted,
            detail=(
                f"workout {workout.key}: about {before:.0f} min for a {session_minutes} min "
                f"session; {'; '.join(changes) or 'nothing could be shortened'}; {outcome}"
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
