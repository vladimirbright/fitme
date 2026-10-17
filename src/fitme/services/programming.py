"""ADR 0011: the weekly strategy, decided deterministically *before* any exercise is chosen,
and sent to `plan_generate` / `plan_revise` as `programming_brief`.

A model asked for "boxing conditioning" tends to collect boxing-flavoured exercises and then
name sessions after them. The brief fixes the frame first, from the profile alone: how many
sessions of how long, which qualities are the plan's main objectives and which secondary,
whether the week should be full-body sessions with different emphases or a split, how much
fatigue the user's other sport leaves room for, and the usual order of a session's blocks.
The model then states each workout's objectives (`Workout.qualities`) and each block's role
(`Block.role`) before it picks exercises, and `services.plan_quality` checks the result
against the same frame.

These are heuristics, not sports science: the numbers are rough budgets with their reasons
next to them, and nothing here is a guard.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from fitme.domain.enums import BlockRole, Preference, Quality
from fitme.llm.context import UserContext
from fitme.services import exercise_search, session_time
from fitme.services.plan_request import PlanRequest

# A session's usual block order (A§6.4): warm-up, fast work while fresh, the main lifts,
# accessories, trunk, conditioning, mobility. A session uses only the parts it needs.
SESSION_ORDER: tuple[BlockRole, ...] = (
    BlockRole.WARM_UP,
    BlockRole.POWER,
    BlockRole.STRENGTH,
    BlockRole.ACCESSORY,
    BlockRole.CORE,
    BlockRole.CONDITIONING,
    BlockRole.MOBILITY,
)
# Main objectives: the first qualities the request (then the sport, focus, preferences)
# names. Two is enough to give each a real share of the week; the rest are secondary.
MAIN_QUALITIES = 2
# Rough minutes per working block (sets, rests and setup) for the block budget: about 10 for
# the upper bound and 15 for the lower one, after a few minutes of warm-up.
_MINUTES_PER_BLOCK_LOW = 15
_MINUTES_PER_BLOCK_HIGH = 10
# Weekly sessions of another sport from which this plan is supporting work with a lower
# fatigue budget (`moderate`), and from which it should stay clearly below a standalone plan
# (`low`).
_SPORT_MODERATE = 2
_SPORT_LOW = 4


def working_blocks(session_minutes: int) -> tuple[int, int]:
    """A rough range of working blocks (singles or supersets) after the warm-up that fits
    `session_minutes`: 60 min -> 4-6, 45 -> 3-4, 30 -> 2-3."""
    low = max(2, round(session_minutes / _MINUTES_PER_BLOCK_LOW))
    high = max(low + 1, round(session_minutes / _MINUTES_PER_BLOCK_HIGH))
    return low, high


def structure(sessions_per_week: int, *, split_wanted: bool) -> str:
    """`split` when the user asked for one; otherwise, with up to three sessions a week,
    full-body sessions that each cover the main regions with a different emphasis (a region
    trained once a week is a split nobody asked for); with more, full-body or upper/lower."""
    if split_wanted:
        return "split_as_requested"
    if sessions_per_week <= 3:
        return "full_body_varied_emphasis"
    return "full_body_or_upper_lower"


def fatigue_budget(sport_sessions_per_week: int | None) -> str:
    """How much room the user's other sport leaves: `normal`, `moderate` or `low`."""
    sessions = sport_sessions_per_week or 0
    if sessions >= _SPORT_LOW:
        return "low"
    if sessions >= _SPORT_MODERATE:
        return "moderate"
    return "normal"


# A split as a whole word — not "split squat", "split jump", "сплит-приседания" — or the
# upper/lower and верх/низ shapes.
_SPLIT_RE = re.compile(
    r"\b(?:split|сплит\w*)\b(?![\s-]*(?:squat|jump|stance|lunge|присед|выпад|прыж))"
    r"|\bupper\s*[/-]?\s*lower\b|\bверх\s*[/-]\s*низ\b",
    re.UNICODE,
)


def split_wanted(context: UserContext, request: str | None) -> bool:
    """A split is what the user asked for: the `split` preference, or the request's words —
    only what it asks *for* (`exercise_search.positive_request`): "no split", "без сплита"
    or "не сплит, а фулбоди" is not a request for one, and neither is a split squat."""
    text = exercise_search.positive_request(request) if request else ""
    return Preference.SPLIT in context.preferences or _SPLIT_RE.search(text) is not None


def session_objectives(sessions_per_week: int, qualities: Sequence[Quality]) -> list[str]:
    """ADR 0016: a suggested main objective per weekly session, spreading the main
    qualities over the week in turn (two sessions, power and agility: one each) rather than
    asking every session to cover all of them. Empty without requested qualities."""
    main = list(qualities[:MAIN_QUALITIES])
    if not main or sessions_per_week < 1:
        return []
    return [main[index % len(main)].value for index in range(sessions_per_week)]


def brief(
    context: UserContext,
    qualities: Sequence[Quality],
    *,
    split_wanted: bool,
    request: PlanRequest | None = None,
) -> dict[str, object]:
    """The `programming_brief` for one plan request. Machine data, no user text. The
    sessions and their length are the context's: ADR 0017's `planning.apply_request` has
    already put the request's own numbers there."""
    low, high = working_blocks(context.session_minutes)
    result: dict[str, object] = {
        "sessions_per_week": context.sessions_per_week,
        "session_minutes": context.session_minutes,
        "working_blocks_per_session": [low, high],
        "main_qualities": [quality.value for quality in qualities[:MAIN_QUALITIES]],
        "secondary_qualities": [quality.value for quality in qualities[MAIN_QUALITIES:]],
        "session_objectives": session_objectives(context.sessions_per_week, qualities),
        "structure": structure(context.sessions_per_week, split_wanted=split_wanted),
        "session_order": [role.value for role in SESSION_ORDER],
        "fatigue_budget": fatigue_budget(context.sport_sessions_per_week),
        # ADR 0016: the app estimates each workout's length and trims an over-long one by
        # priority (`services.time_budget`); the model plans to fit it in the first place.
        "time_budget": {
            "target_minutes": context.session_minutes,
            "estimated_by": "app",
            # The estimate's uncertainty, not extra time: the target stays the session length.
            "estimate_uncertainty": round(session_time.DEFAULT_MODEL.tolerance - 1, 2),
            "over_target": "lowest-priority volume is cut set by set, then blocks; rests kept",
        },
    }
    if request is not None and request.weekdays:
        # ADR 0017: the days the request names (0 = Monday) are the schedule.
        result["weekdays"] = list(request.weekdays)
    if request is not None and request.activity is not None:
        # ADR 0017: what the request is about. `practice`: the sessions are the activity
        # itself (warm-up, the activity as the main work, cool-down); `support`: work for an
        # activity done elsewhere, whose skills and sparring the catalog doesn't hold.
        result["activity"] = {
            "name": request.activity.value,
            "mode": "practice" if request.practice else "support",
        }
    if request is not None and request.outdoor:
        result["setting"] = "outdoors"
    if context.sport is not None:
        result["supporting_sport"] = {
            "sport": context.sport.value,
            "sessions_per_week": context.sport_sessions_per_week,
        }
    return result
