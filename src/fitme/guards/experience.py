"""Advanced exercises need training experience (catalog `level`, added 2026-10-15).

An exercise marked `advanced` in the catalog (advanced gymnastics, Olympic lifts, single-limb
loaded variations) may be prescribed only to a user who reported at least two years of
training. An unknown experience fails closed: it is treated as too little. This only ever
removes options; it never allows anything another guard blocks.
"""

from __future__ import annotations

from fitme.domain.catalog import Exercise
from fitme.domain.enums import ExerciseLevel, Experience
from fitme.domain.guard_types import GuardVerdict

RULE = "plan.experience_level"
ADVANCED_EXPERIENCE: frozenset[Experience] = frozenset({Experience.Y2_TO_5Y, Experience.GT_5Y})


def exercise_level_allowed(exercise: Exercise, experience: Experience | None) -> GuardVerdict:
    if exercise.level != ExerciseLevel.ADVANCED:
        return GuardVerdict(rule=RULE, ok=True, detail=f"{exercise.id}: general level")
    if experience in ADVANCED_EXPERIENCE:
        return GuardVerdict(
            rule=RULE, ok=True, detail=f"{exercise.id}: advanced, experience {experience}"
        )
    shown = "unknown" if experience is None else experience.value
    return GuardVerdict(
        rule=RULE,
        ok=False,
        detail=f"{exercise.id} is an advanced exercise; needs 2+ years of training ({shown})",
    )
