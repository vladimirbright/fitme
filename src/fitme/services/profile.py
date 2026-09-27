"""The setup questionnaire (A§5.1) and `/profile` read/write (A§6.2).

Owns the step order and the (mostly linear, with two conditionally-skipped steps)
navigation, and writes every answer to `profiles`/`screening_flags`/`screening_notes` as soon
as it's known ("answers are saved step by step", A§5.1). `setup_progress.step` — not aiogram
FSM memory — is the single source of truth for where a conversation currently is, so a bot
restart mid-setup resumes exactly here (M5, A§4.6 "keep setup state in the DB").

Bot handlers own *rendering* (the i18n text and keyboard for a step); this module owns the
step machine and the DB reads/writes. `/profile` edits reuse the same machine: fixing one
field jumps to that field's step and continues forward through the ordinary flow to
`summary` again, so changing a screening answer naturally re-runs the remaining screening
steps (A§5.1, A§6.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from fitme import clock
from fitme.db.connection import Database
from fitme.db.controllers.profile import (
    delete_setup_progress,
    insert_screening_note,
    upsert_profile,
    upsert_screening_flag,
    upsert_setup_progress,
)
from fitme.db.controllers.users import update_user_language, update_user_timezone
from fitme.db.records import ProfileRecord, ScreeningFlagRecord, ScreeningNoteRecord
from fitme.db.selectors.profile import (
    get_profile,
    get_screening_flag,
    get_setup_progress,
    list_screening_flags,
    list_screening_notes,
)
from fitme.db.selectors.users import get_user
from fitme.domain.enums import (
    AREA_FLAGS,
    NEEDS_CLEARANCE_FLAGS,
    RED_FLAGS,
    Location,
    ScreeningFlag,
)

# --- Step order (A§5.1) --------------------------------------------------------------------
#
# Two steps are conditionally skipped by `visible_steps`: "screening_clearance" (only shown
# when a red flag or `other_unlisted` was answered "yes") and "equipment" (only shown for
# `home_equipment`). Everything else is always shown, in this order. Unlike A§5.1's literal
# step numbering, the clearance question is asked once, after every screening input
# (including the free-text "other") has been collected, rather than immediately after the red
# flags — simpler to implement correctly (one clearance question, not a conditional second
# one after "other"), and functionally equivalent: `guards.screening.plan_allowed` only cares
# about the final stored answer, not the order it was collected in.
RED_FLAG_STEPS: tuple[tuple[str, ScreeningFlag], ...] = tuple(
    (f"screening_{flag.value}", flag) for flag in sorted(RED_FLAGS, key=lambda f: f.value)
)
_RED_FLAG_STEP_NAMES = tuple(step for step, _flag in RED_FLAG_STEPS)
STEP_TO_RED_FLAG: dict[str, ScreeningFlag] = dict(RED_FLAG_STEPS)

STEP_ORDER: tuple[str, ...] = (
    "language",
    "timezone",
    "age",
    "weight",
    "experience_duration",
    "experience_barbell",
    *_RED_FLAG_STEP_NAMES,
    "screening_areas",
    "screening_other",
    "screening_clearance",
    "preferences",
    "focus",
    "location",
    "equipment",
    "frequency",
    "session_length",
    "summary",
)

# `/profile` "Fix" jumps here, then continues forward through the ordinary flow to `summary`.
FIELD_TO_STEP: dict[str, str] = {
    "language": "language",
    "timezone": "timezone",
    "age": "age",
    "weight": "weight",
    "experience": "experience_duration",
    "barbell_experience": "experience_barbell",
    "screening": _RED_FLAG_STEP_NAMES[0],
    "preferences": "preferences",
    "focus": "focus",
    "location": "location",
    "equipment": "equipment",
    "frequency": "frequency",
    "session_length": "session_length",
}


@dataclass(frozen=True, slots=True)
class StepContext:
    location: str | None
    needs_clearance: bool


def _visible(step: str, ctx: StepContext) -> bool:
    if step == "screening_clearance":
        return ctx.needs_clearance
    if step == "equipment":
        return ctx.location == Location.HOME_EQUIPMENT.value
    return True


def visible_steps(ctx: StepContext) -> tuple[str, ...]:
    return tuple(step for step in STEP_ORDER if _visible(step, ctx))


def _anchor_index(step: str, steps: tuple[str, ...]) -> int:
    """`step`'s index in `steps`, or (if `step` is currently hidden, e.g. "equipment" after
    switching away from `home_equipment`) the index of the nearest earlier visible step."""
    if step in steps:
        return steps.index(step)
    order_pos = STEP_ORDER.index(step)
    for candidate in reversed(STEP_ORDER[: order_pos + 1]):
        if candidate in steps:
            return steps.index(candidate)
    return 0


def next_step(step: str, ctx: StepContext) -> str | None:
    """The next visible step after `step`, or `None` if `step` is the last one (`summary`)."""
    steps = visible_steps(ctx)
    idx = _anchor_index(step, steps)
    return steps[idx + 1] if idx + 1 < len(steps) else None


def prev_step(step: str, ctx: StepContext) -> str | None:
    """The previous visible step before `step`, or `None` if `step` is the first one."""
    steps = visible_steps(ctx)
    idx = _anchor_index(step, steps)
    return steps[idx - 1] if idx > 0 else None


def needs_clearance(flags: dict[ScreeningFlag, str]) -> bool:
    return any(flags.get(flag) == "yes" for flag in NEEDS_CLEARANCE_FLAGS)


# --- Snapshot / context building ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    language: str
    timezone: str | None
    profile: ProfileRecord | None
    flags: list[ScreeningFlagRecord]
    notes: list[ScreeningNoteRecord]


async def get_snapshot(db: Database, user_id: int) -> ProfileSnapshot:
    async with db.read() as conn:
        user = await get_user(conn, user_id)
        profile = await get_profile(conn, user_id)
        flags = await list_screening_flags(conn, user_id)
        notes = await list_screening_notes(conn, user_id)
    assert user is not None
    return ProfileSnapshot(
        language=user.language, timezone=user.timezone, profile=profile, flags=flags, notes=notes
    )


async def build_step_context(db: Database, user_id: int) -> StepContext:
    snapshot = await get_snapshot(db, user_id)
    flag_values = {ScreeningFlag(f.flag): f.value for f in snapshot.flags}
    location = snapshot.profile.location if snapshot.profile is not None else None
    return StepContext(location=location, needs_clearance=needs_clearance(flag_values))


# --- Setup progress marker -------------------------------------------------------------------


async def get_step(db: Database, user_id: int) -> str | None:
    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    return None if progress is None else progress.step


async def get_progress_data(db: Database, user_id: int) -> dict[str, object]:
    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    return {} if progress is None else progress.data


async def set_step(
    db: Database, user_id: int, step: str, *, data: dict[str, object] | None = None
) -> None:
    async with db.transaction() as conn:
        await upsert_setup_progress(conn, user_id=user_id, step=step, data=data or {})


async def clear_progress(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await delete_setup_progress(conn, user_id)


# --- Answer writers (A§5.1: saved step by step) --------------------------------------------


def _profile_updates(profile: ProfileRecord | None) -> dict[str, object]:
    """The current row's columns, ready to feed back into `upsert_profile` unchanged except
    for whatever the caller overrides. `completed_at` is parsed back into a `datetime`: the
    selector returns it as the already-formatted TEXT column (A§4.2), but the controller's
    parameter is a `datetime | None` that it formats itself on the way in."""
    if profile is None:
        return {
            "age_bucket": None,
            "weight_bucket": None,
            "experience": None,
            "barbell_experience": None,
            "preferences": [],
            "location": None,
            "equipment": [],
            "sessions_per_week": None,
            "session_minutes": None,
            "focus": None,
            "completed_at": None,
        }
    return {
        "age_bucket": profile.age_bucket,
        "weight_bucket": profile.weight_bucket,
        "experience": profile.experience,
        "barbell_experience": profile.barbell_experience,
        "preferences": list(profile.preferences),
        "location": profile.location,
        "equipment": list(profile.equipment),
        "sessions_per_week": profile.sessions_per_week,
        "session_minutes": profile.session_minutes,
        "focus": profile.focus,
        "completed_at": (
            None if profile.completed_at is None else clock.parse_timestamp(profile.completed_at)
        ),
    }


async def update_profile_fields(db: Database, user_id: int, **updates: object) -> None:
    """Read-modify-write one or more `profiles` columns, keeping every other field untouched.
    `upsert_profile` replaces the whole row, so every call here re-reads the current row
    first (A§5.1: setup saves step by step, so most calls only ever change one field)."""
    async with db.transaction() as conn:
        current = await get_profile(conn, user_id)
        merged = _profile_updates(current)
        merged.update(updates)
        completed_at = merged.pop("completed_at")
        await upsert_profile(
            conn,
            user_id=user_id,
            completed_at=completed_at,  # type: ignore[arg-type]
            **merged,  # type: ignore[arg-type]
        )


async def set_language(db: Database, user_id: int, language: str) -> None:
    async with db.transaction() as conn:
        await update_user_language(conn, user_id, language)


async def set_timezone(db: Database, user_id: int, timezone: str) -> None:
    async with db.transaction() as conn:
        await update_user_timezone(conn, user_id, timezone)


async def set_screening_flag(
    db: Database, user_id: int, flag: ScreeningFlag, value: str, *, clearance: str | None = None
) -> None:
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn, user_id=user_id, flag=flag.value, value=value, clearance=clearance
        )


async def set_areas(db: Database, user_id: int, selected: set[ScreeningFlag]) -> None:
    """Every area flag gets an explicit yes/no (not just the selected ones): silence isn't
    meant to be consent even for a flag that only gates exercise selection, not just the
    red-flag "screening_incomplete" refusal (AGENTS.md §2)."""
    async with db.transaction() as conn:
        for flag in sorted(AREA_FLAGS, key=lambda f: f.value):
            value = "yes" if flag in selected else "no"
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value=value, clearance=None
            )


def _next_step_after_screening_other(
    flags: list[ScreeningFlagRecord], profile: ProfileRecord | None
) -> str:
    flag_values = {ScreeningFlag(f.flag): f.value for f in flags}
    location = profile.location if profile is not None else None
    ctx = StepContext(location=location, needs_clearance=needs_clearance(flag_values))
    return next_step("screening_other", ctx) or "summary"


async def submit_other_note(db: Database, user_id: int, text: str) -> str:
    """A non-blank "other" note (A§5.1, B2). Always sets `other_unlisted = yes` and advances
    past `screening_other` in the *same transaction* as the write — halt or not, the note is
    this step's definitive answer, so nothing may later observe `setup_progress.step ==
    "screening_other"` and reopen it via a stale Skip press. The note text itself is stored
    in `screening_notes`, never in `screening_flags` (AGENTS.md §5: never sent to the LLM).
    Returns the step advanced to."""
    async with db.transaction() as conn:
        await insert_screening_note(conn, user_id=user_id, text=text)
        await upsert_screening_flag(
            conn,
            user_id=user_id,
            flag=ScreeningFlag.OTHER_UNLISTED.value,
            value="yes",
            clearance=None,
        )
        flags = await list_screening_flags(conn, user_id)
        profile = await get_profile(conn, user_id)
        nxt = _next_step_after_screening_other(flags, profile)
        await upsert_setup_progress(conn, user_id=user_id, step=nxt, data={})
    return nxt


async def skip_other_note(db: Database, user_id: int) -> str:
    """ "Skip" for `screening_other` (A§5.1). Sets `other_unlisted = no`, *unless* it is
    already "yes" (belt-and-braces, B2): a note that already answered this step — including
    one that also halted the session — is never downgraded back to "no" by a concurrently
    arriving or stale Skip press. A deliberate `/profile` re-run resets the flag first
    (`reset_other_unlisted`), so a genuine re-answer via Skip still works there. Always
    advances past the step (Skip is itself a valid answer). Returns the step advanced to."""
    async with db.transaction() as conn:
        current = await get_screening_flag(conn, user_id, ScreeningFlag.OTHER_UNLISTED.value)
        if current is None or current.value != "yes":
            await upsert_screening_flag(
                conn,
                user_id=user_id,
                flag=ScreeningFlag.OTHER_UNLISTED.value,
                value="no",
                clearance=None,
            )
        flags = await list_screening_flags(conn, user_id)
        profile = await get_profile(conn, user_id)
        nxt = _next_step_after_screening_other(flags, profile)
        await upsert_setup_progress(conn, user_id=user_id, step=nxt, data={})
    return nxt


async def reset_other_unlisted(db: Database, user_id: int) -> None:
    """A deliberate `/profile` re-run of screening (A§6.2) may legitimately set
    `other_unlisted` back to "no" via Skip — reset it here, before the red-flag steps run
    again, so `skip_other_note`'s never-downgrade rule doesn't block that intentional
    change."""
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn,
            user_id=user_id,
            flag=ScreeningFlag.OTHER_UNLISTED.value,
            value="no",
            clearance=None,
        )


async def set_clearance(db: Database, user_id: int, clearance: str) -> None:
    """Applies one clearance answer to every needs-clearance flag currently answered "yes"
    (A§5.1: a single doctor-clearance question covers every flag that triggered it)."""
    async with db.transaction() as conn:
        flags = await list_screening_flags(conn, user_id)
        for record in flags:
            flag = ScreeningFlag(record.flag)
            if flag in NEEDS_CLEARANCE_FLAGS and record.value == "yes":
                await upsert_screening_flag(
                    conn, user_id=user_id, flag=flag.value, value="yes", clearance=clearance
                )


async def complete_setup(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        current = await get_profile(conn, user_id)
        merged = _profile_updates(current)
        merged["completed_at"] = clock.now()
        await upsert_profile(conn, user_id=user_id, **merged)  # type: ignore[arg-type]
        await delete_setup_progress(conn, user_id)


__all__ = [
    "FIELD_TO_STEP",
    "STEP_ORDER",
    "STEP_TO_RED_FLAG",
    "ProfileSnapshot",
    "StepContext",
    "build_step_context",
    "clear_progress",
    "complete_setup",
    "get_progress_data",
    "get_snapshot",
    "get_step",
    "needs_clearance",
    "next_step",
    "prev_step",
    "reset_other_unlisted",
    "set_areas",
    "set_clearance",
    "set_language",
    "set_screening_flag",
    "set_step",
    "set_timezone",
    "skip_other_note",
    "submit_other_note",
    "update_profile_fields",
    "visible_steps",
]
