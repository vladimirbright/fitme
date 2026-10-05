"""`/stats` and `/system` (A§6.2, A§6.7): read-only summaries, no LLM calls.

Both are plain aggregation over existing selectors — no guard, no decision, nothing written.
`/stats` is neutral by construction (AGENTS.md §4: no streaks, no shaming): it reports counts
and current numbers, never a run length or a missed-day count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from fitme import clock
from fitme.config.content import content_version as compute_content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.selectors.decisions import list_llm_calls_since
from fitme.db.selectors.plans import get_default_plan, get_latest_plan_version
from fitme.db.selectors.profile import get_profile
from fitme.db.selectors.training import (
    count_workout_sessions_for_user,
    list_set_logs_for_session,
    list_workout_sessions_for_user,
    recent_session_outcomes_by_exercise,
)
from fitme.db.selectors.users import get_user
from fitme.domain.models import Plan
from fitme.services import training as training_service

_STATS_WEEKS = 4
_TOP_LIFTS_COUNT = 3
_UPCOMING_COUNT = 3
_UPCOMING_SEARCH_DAYS = 21  # two-and-change weeks of schedule to find 3 upcoming workouts

# Lexically smaller than any real `fitme.clock` timestamp: a safe "since the beginning" bound
# for `list_llm_calls_since` without adding a separate "since=None" code path there.
_EPOCH = "0000-01-01T00:00:00.000000Z"


@dataclass(frozen=True, slots=True)
class WeekSummary:
    completed: int
    scheduled: int


@dataclass(frozen=True, slots=True)
class TopLift:
    exercise_id: str
    current_kg: float
    change_kg: float  # vs. the previous completed session; 0.0 with only one data point


@dataclass(frozen=True, slots=True)
class UpcomingWorkout:
    date: str  # ISO date (YYYY-MM-DD) in the user's timezone
    workout_key: str


@dataclass(frozen=True, slots=True)
class ShortStats:
    """A§6.7: the whole `/stats` reply."""

    weeks: WeekSummary
    volume_kg: float
    top_lifts: list[TopLift] = field(default_factory=list)
    upcoming: list[UpcomingWorkout] = field(default_factory=list)


async def short_stats(db: Database, user_id: int) -> ShortStats:
    since = clock.format_timestamp(clock.now() - timedelta(weeks=_STATS_WEEKS))
    async with db.read() as conn:
        user = await get_user(conn, user_id)
        profile = await get_profile(conn, user_id)
        sessions = await list_workout_sessions_for_user(conn, user_id)
        recent_completed = [
            session
            for session in sessions
            if session.status == "completed"
            and session.finished_at is not None
            and session.finished_at >= since
        ]
        total_volume_kg = 0.0
        for session in recent_completed:
            rows = await list_set_logs_for_session(conn, session.id)
            total_volume_kg += training_service.volume_kg(rows)

        outcomes = await recent_session_outcomes_by_exercise(conn, user_id, limit=2)

        default_plan = await get_default_plan(conn, user_id)
        plan_body: Plan | None = None
        if default_plan is not None:
            version = await get_latest_plan_version(conn, default_plan.id)
            if version is not None:
                try:
                    plan_body = Plan.model_validate(version.body)
                except ValueError:
                    plan_body = None

    # The default plan's own schedule is what the user actually committed to; the profile's
    # frequency is only the default a new plan starts from.
    scheduled_per_week = (
        len(plan_body.schedule)
        if plan_body is not None and plan_body.schedule
        else (profile.sessions_per_week if profile is not None else None)
    )
    scheduled = _STATS_WEEKS * scheduled_per_week if scheduled_per_week is not None else 0

    top_lifts: list[TopLift] = []
    for exercise_id, session_outcomes in outcomes.items():
        if not session_outcomes or session_outcomes[0].planned_load_kg is None:
            continue
        current = session_outcomes[0].planned_load_kg
        previous = session_outcomes[1].planned_load_kg if len(session_outcomes) > 1 else None
        change = 0.0 if previous is None else current - previous
        top_lifts.append(TopLift(exercise_id=exercise_id, current_kg=current, change_kg=change))
    top_lifts.sort(key=lambda item: item.current_kg, reverse=True)
    top_lifts = top_lifts[:_TOP_LIFTS_COUNT]

    upcoming: list[UpcomingWorkout] = []
    if plan_body is not None and plan_body.schedule:
        tz = ZoneInfo(user.timezone) if user is not None and user.timezone else ZoneInfo("UTC")
        today = clock.now().astimezone(tz).date()
        by_weekday = {day.weekday: day.workout_key for day in plan_body.schedule}
        for offset in range(_UPCOMING_SEARCH_DAYS):
            day = today + timedelta(days=offset)
            workout_key = by_weekday.get(day.weekday())
            if workout_key is not None:
                upcoming.append(UpcomingWorkout(date=day.isoformat(), workout_key=workout_key))
            if len(upcoming) >= _UPCOMING_COUNT:
                break

    return ShortStats(
        weeks=WeekSummary(completed=len(recent_completed), scheduled=scheduled),
        volume_kg=round(total_volume_kg, 1),
        top_lifts=top_lifts,
        upcoming=upcoming,
    )


# --- /system (A§6.2) -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LlmWindowStats:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None: no call in the window has a configured price


@dataclass(frozen=True, slots=True)
class SystemStats:
    db_size_bytes: int
    sessions_logged: int
    llm_today: LlmWindowStats
    llm_7d: LlmWindowStats
    llm_30d: LlmWindowStats
    llm_all: LlmWindowStats
    content_version: str


def _db_size_bytes(settings: Settings) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = settings.db_path.with_name(settings.db_path.name + suffix)
        if path.exists():
            total += path.stat().st_size
    return total


async def _window_stats(db: Database, since: str) -> LlmWindowStats:
    """`cost_usd` sums each call's own `cost_estimate_usd` (computed once, at call time, from
    whatever price table was configured then — A§8.5 rule 5). `None` only when *no* call in
    the window has a price (prices were never configured, or none of these calls' models are
    priced); a mix of priced and unpriced calls still reports the sum of the known ones,
    which is a floor on the real cost, not a guess."""
    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since)
    input_tokens = sum(call.input_tokens for call in calls)
    output_tokens = sum(call.output_tokens for call in calls)
    known_costs = [call.cost_estimate_usd for call in calls if call.cost_estimate_usd is not None]
    cost_usd = sum(known_costs) if known_costs else None
    return LlmWindowStats(
        calls=len(calls), input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
    )


async def system_stats(db: Database, settings: Settings, user_id: int) -> SystemStats:
    now = clock.now()
    today_start = clock.format_timestamp(now.replace(hour=0, minute=0, second=0, microsecond=0))
    since_7d = clock.format_timestamp(now - timedelta(days=7))
    since_30d = clock.format_timestamp(now - timedelta(days=30))

    async with db.read() as conn:
        sessions_logged = await count_workout_sessions_for_user(conn, user_id)

    return SystemStats(
        db_size_bytes=_db_size_bytes(settings),
        sessions_logged=sessions_logged,
        llm_today=await _window_stats(db, today_start),
        llm_7d=await _window_stats(db, since_7d),
        llm_30d=await _window_stats(db, since_30d),
        llm_all=await _window_stats(db, _EPOCH),
        content_version=compute_content_version(),
    )


# --- /app/stats charts (A§9.1) --------------------------------------------------------------


_CHART_WEEKS = 12
_MAX_CHART_EXERCISES = 6


@dataclass(frozen=True, slots=True)
class WeekPoint:
    week_start: str  # ISO date (Monday)
    value: float


@dataclass(frozen=True, slots=True)
class LoadPoint:
    date: str  # ISO date
    kg: float


@dataclass(frozen=True, slots=True)
class ChartData:
    weekly_volume: list[WeekPoint]
    sessions_per_week: list[WeekPoint]
    load_over_time: dict[str, list[LoadPoint]]


async def chart_data(db: Database, user_id: int) -> ChartData:
    """The data behind `/app/stats`'s three charts (A§9.1): weekly training volume, sessions
    per week, and working load over time for the exercises with the most logged history.
    Computed straight from `set_logs`/`workout_sessions` — no caching, no separate table."""
    async with db.read() as conn:
        sessions = await list_workout_sessions_for_user(conn, user_id)
        completed = [
            session
            for session in sessions
            if session.status == "completed" and session.finished_at is not None
        ]
        completed.sort(key=lambda session: session.finished_at or "")

        volume_by_week: dict[str, float] = {}
        sessions_by_week: dict[str, int] = {}
        load_by_exercise: dict[str, list[LoadPoint]] = {}
        for session in completed:
            assert session.finished_at is not None
            finished = clock.parse_timestamp(session.finished_at)
            week_start = (finished - timedelta(days=finished.weekday())).date().isoformat()
            sessions_by_week[week_start] = sessions_by_week.get(week_start, 0) + 1

            rows = await list_set_logs_for_session(conn, session.id)
            best_by_exercise: dict[str, float] = {}
            for row in rows:
                if row.actual_reps is None or row.actual_load_kg is None:
                    continue
                best_by_exercise[row.exercise_id] = max(
                    best_by_exercise.get(row.exercise_id, 0.0), row.actual_load_kg
                )
            session_volume = training_service.volume_kg(rows)
            volume_by_week[week_start] = volume_by_week.get(week_start, 0.0) + session_volume
            date_str = finished.date().isoformat()
            for exercise_id, kg in best_by_exercise.items():
                load_by_exercise.setdefault(exercise_id, []).append(LoadPoint(date=date_str, kg=kg))

    weeks_sorted = sorted(set(volume_by_week) | set(sessions_by_week))[-_CHART_WEEKS:]
    weekly_volume = [
        WeekPoint(week_start=week, value=round(volume_by_week.get(week, 0.0), 1))
        for week in weeks_sorted
    ]
    sessions_per_week = [
        WeekPoint(week_start=week, value=float(sessions_by_week.get(week, 0)))
        for week in weeks_sorted
    ]
    top_exercises = sorted(
        load_by_exercise, key=lambda exercise_id: len(load_by_exercise[exercise_id]), reverse=True
    )[:_MAX_CHART_EXERCISES]
    load_over_time = {exercise_id: load_by_exercise[exercise_id] for exercise_id in top_exercises}
    return ChartData(
        weekly_volume=weekly_volume,
        sessions_per_week=sessions_per_week,
        load_over_time=load_over_time,
    )


__all__ = [
    "ChartData",
    "LlmWindowStats",
    "LoadPoint",
    "ShortStats",
    "SystemStats",
    "TopLift",
    "UpcomingWorkout",
    "WeekPoint",
    "WeekSummary",
    "chart_data",
    "short_stats",
    "system_stats",
]
