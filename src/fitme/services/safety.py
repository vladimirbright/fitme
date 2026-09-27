"""The halt path (A§6.6) and hold clearing, in one place so every trigger goes through the
same code: a stop word in any free text (M5), and — M7 — the precheck "Yes", the pain button,
a `result_parse` safety signal and a stop word typed during a workout.

Order matters (A§6.6): halt the session, create the hold, log the decision — all in one
transaction — then the caller sends the fixed, non-LLM message. `halt()` always halts the
user's active workout session (draft, in review or in progress), whether or not the caller
names one: a hold and a still-running session must never coexist, whatever path raised the
hold (a stop word in a command's arguments halts the workout exactly like the pain button).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from fitme import clock
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.training import (
    clear_health_hold,
    finish_workout_session,
    insert_health_hold,
)
from fitme.db.records import HealthHoldRecord
from fitme.db.selectors.decisions import get_session_halt_decision_for_hold
from fitme.db.selectors.training import get_active_workout_session, list_open_health_holds
from fitme.db.selectors.users import get_user
from fitme.domain.enums import DecisionKind, HealthHoldReason
from fitme.domain.guard_types import GuardVerdict
from fitme.guards import stop_words
from fitme.services.decisions import record_decision

_MIN_HOURS_BEFORE_CLEAR = 12
_SESSION_HALTED = "halted"


@dataclass(frozen=True, slots=True)
class HaltResult:
    hold_id: int
    decision_id: int
    # The workout session this halt stopped (A§6.6 step 1), if one was active.
    session_id: int | None = None


async def _timezone_in_effect(db: Database, user_id: int) -> str | None:
    async with db.read() as conn:
        user = await get_user(conn, user_id)
    return None if user is None else user.timezone


async def halt(
    db: Database,
    *,
    user_id: int,
    reason: HealthHoldReason,
    guards_fired: list[GuardVerdict],
    user_report: dict[str, object],
) -> HaltResult:
    """A§6.6, steps 1-3, in one transaction (so a crash can never leave a halted session
    without its hold, or a hold without its decision): set the active workout session (if
    any) to `halted` with `halt_reason`, open a `health_hold` tied to it, and log
    `decision(kind=session_halt)`. The caller then sends the fixed halt message; no LLM call
    is ever made on this path.

    The halt decision's `user_report` always carries `hold_id`, the halted `session_id` (or
    `None`) and the `timezone` in effect right now (A§6.6: "same day" is evaluated in the
    timezone in effect *when the hold was created*, read back later by `can_clear` — no
    schema change, since `user_report` is the sanctioned free-form JSON column for exactly
    this, A§4.3).
    """
    timezone = await _timezone_in_effect(db, user_id)
    async with db.transaction() as conn:
        active = await get_active_workout_session(conn, user_id)
        session_id = None if active is None else active.id
        if session_id is not None:
            await finish_workout_session(
                conn, session_id, status=_SESSION_HALTED, halt_reason=reason.value
            )
        hold_id = await insert_health_hold(
            conn, user_id=user_id, reason=reason.value, source_session_id=session_id
        )
        full_report: dict[str, object] = {
            **user_report,
            "hold_id": hold_id,
            "session_id": session_id,
            "timezone": timezone,
        }
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.SESSION_HALT.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=full_report,
            proposal=None,
            guards_fired=[verdict.model_dump() for verdict in guards_fired],
        )
    return HaltResult(hold_id=hold_id, decision_id=decision_id, session_id=session_id)


async def record_incoming_text(
    db: Database, *, user_id: int, session_id: int | None, text: str
) -> int:
    """Every free-text message from the owner is saved to `chat_messages` (A§6.3), purged by
    the retention job. Called *before* the stop-word scan, so even a halting message is kept
    (it's the record the halt decision points back to)."""
    async with db.transaction() as conn:
        return await insert_chat_message(
            conn, user_id=user_id, session_id=session_id, direction="in", text=text
        )


async def scan_and_maybe_halt(
    db: Database, *, user_id: int, lang: str, text: str
) -> HaltResult | None:
    """The pre-LLM stop-word check (A§6.3, A§7): `None` means the text passed and normal
    handling continues. A hit runs the full halt path and returns its result."""
    hit = stop_words.scan(text, lang)
    if hit is None:
        return None
    verdict = stop_words.to_verdict(hit)
    return await halt(
        db,
        user_id=user_id,
        reason=HealthHoldReason.STOP_WORD,
        guards_fired=[verdict],
        user_report={"trigger": "stop_word", "category": hit.category},
    )


async def open_holds(db: Database, user_id: int) -> list[HealthHoldRecord]:
    async with db.read() as conn:
        return await list_open_health_holds(conn, user_id)


def _local_date(iso_timestamp: str, timezone: str | None) -> date:
    dt = clock.parse_timestamp(iso_timestamp)
    tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    return dt.astimezone(tz).date()


async def _hold_timezone(db: Database, hold_id: int) -> str | None:
    """The timezone recorded on the `session_halt` decision that created `hold_id`, or
    `None` (falls back to UTC) if no such decision is found — e.g. a hold created directly
    by a test fixture rather than through `halt()`."""
    async with db.read() as conn:
        decision = await get_session_halt_decision_for_hold(conn, hold_id)
    if decision is None or decision.user_report is None:
        return None
    timezone = decision.user_report.get("timezone")
    return timezone if isinstance(timezone, str) else None


async def can_clear(db: Database, hold: HealthHoldRecord) -> bool:
    """A§6.6: "Holds cannot be cleared on the same day" — the calendar day the hold was
    created on, in the timezone that was in effect *at that time* (read back from the
    triggering `session_halt` decision's `user_report`, not the user's current timezone
    setting — changing the timezone must not shorten a hold). At least
    `_MIN_HOURS_BEFORE_CLEAR` hours must also have passed, for the same reason.
    """
    now = clock.now()
    if now - clock.parse_timestamp(hold.created_at) < timedelta(hours=_MIN_HOURS_BEFORE_CLEAR):
        return False
    timezone = await _hold_timezone(db, hold.id)
    today = now.astimezone(ZoneInfo(timezone) if timezone else ZoneInfo("UTC")).date()
    return today > _local_date(hold.created_at, timezone)


async def clear(db: Database, *, user_id: int, hold_id: int) -> int:
    """Clears one hold and logs the clearing as a `hold_clear` decision (A§6.6). Returns the
    decision id."""
    async with db.transaction() as conn:
        await clear_health_hold(conn, hold_id)
    return await record_decision(
        db,
        user_id=user_id,
        kind=DecisionKind.HOLD_CLEAR,
        prompt_template=None,
        prompt_version=None,
        model=None,
        content_version=content_version(),
        llm_input=None,
        user_report={"hold_id": hold_id},
        proposal=None,
        guards_fired=[],
    )
