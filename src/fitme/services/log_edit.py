"""Correcting already-logged sets of a finished session (ADR 0003), and undoing that.

The owner tells the bot "on Monday I actually did 8, 8, 6 on bench at 60": the `assistant`
agent turns that into `FixLoggedSet` ops, and this module applies them — or refuses — with
the same plausibility guard a parsed training result goes through (`guards.plausibility`,
A§6.5.1). That matters because logged loads feed the historical max, which the ceiling guard
builds on: a typo here must not be able to raise what the guards allow later.

Only *finished* sessions (completed, aborted, halted) can be corrected; an in-progress
workout's results go through `/train`'s own flow. Each saved correction is one
`decision(kind=user_edit)` with `user_report.action = "fix_logged_sets"` holding every changed
row's before/after values (AGENTS.md §6), which is also what Undo restores from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.training import restore_set_log, update_set_log_actual
from fitme.db.records import SetLogRecord
from fitme.db.selectors.decisions import get_decision
from fitme.db.selectors.training import (
    get_workout_session,
    historical_max_by_exercise_before_session,
    list_set_logs_for_session,
)
from fitme.domain.enums import DecisionKind, WorkoutSessionStatus
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load
from fitme.guards.plausibility import check_parsed_load
from fitme.services.plan_edit import PromptMeta, prompt_columns

FIX_ACTION = "fix_logged_sets"
UNDO_ACTION = "undo_fix_logged_sets"
_SOURCE = "free_text"

FINISHED_STATUSES = frozenset(
    {
        WorkoutSessionStatus.COMPLETED.value,
        WorkoutSessionStatus.ABORTED.value,
        WorkoutSessionStatus.HALTED.value,
    }
)


@dataclass(frozen=True, slots=True)
class SetFix:
    exercise_id: str
    set_number: int  # 1-based, counted over this exercise's rows in the session
    reps: int
    load_kg: float | None


class LogEditStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"  # not this user's session, or unknown
    NOT_FINISHED = "not_finished"  # draft / in progress: use /train
    NO_SUCH_SET = "no_such_set"
    IMPLAUSIBLE = "implausible"  # the plausibility guard refused a load
    STALE = "stale"  # undo: the rows changed since the correction


@dataclass(frozen=True, slots=True)
class SetChange:
    set_log_id: int
    exercise_id: str
    set_number: int
    before_reps: int | None
    before_kg: float | None
    after_reps: int | None
    after_kg: float | None


@dataclass(frozen=True, slots=True)
class LogEditResult:
    status: LogEditStatus
    session_id: int | None = None
    decision_id: int | None = None
    changes: tuple[SetChange, ...] = ()
    details: tuple[str, ...] = ()


def numbered_rows(rows: Sequence[SetLogRecord]) -> list[tuple[int, SetLogRecord]]:
    """`(set_number, row)` per row: 1-based, counted per exercise in creation order (one
    exercise may appear in two blocks of a workout, so `set_index` alone is ambiguous)."""
    seen: dict[str, int] = {}
    numbered: list[tuple[int, SetLogRecord]] = []
    for row in rows:
        seen[row.exercise_id] = seen.get(row.exercise_id, 0) + 1
        numbered.append((seen[row.exercise_id], row))
    return numbered


def _row_state(row: SetLogRecord) -> dict[str, object]:
    return {
        "actual_reps": row.actual_reps,
        "actual_load_kg": row.actual_load_kg,
        "skipped": row.skipped,
        "source": row.source,
    }


async def fix_logged_sets(
    db: Database,
    user_id: int,
    session_id: int,
    fixes: Sequence[SetFix],
    *,
    report_extra: Mapping[str, object] | None = None,
    prompt: PromptMeta | None = None,
) -> LogEditResult:
    """Apply every fix or none: an unknown set or an implausible load refuses the whole
    batch (nothing written)."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        session = await get_workout_session(conn, session_id)
        if session is None or session.user_id != user_id:
            return LogEditResult(status=LogEditStatus.NOT_FOUND)
        if session.status not in FINISHED_STATUSES:
            return LogEditResult(status=LogEditStatus.NOT_FINISHED, session_id=session_id)
        rows = numbered_rows(await list_set_logs_for_session(conn, session_id))
        by_key = {(row.exercise_id, number): row for number, row in rows}
        history_max = await historical_max_by_exercise_before_session(conn, user_id, session_id)

        verdicts: list[GuardVerdict] = []
        problems: list[str] = []
        planned: list[tuple[SetFix, SetLogRecord]] = []
        for fix in fixes:
            row = by_key.get((fix.exercise_id, fix.set_number))
            if row is None:
                return LogEditResult(
                    status=LogEditStatus.NO_SUCH_SET,
                    session_id=session_id,
                    details=(f"{fix.exercise_id} set {fix.set_number}",),
                )
            exercise = catalog.by_id(fix.exercise_id)
            if fix.load_kg is None and (exercise is None or exercise.kg_loadable):
                # Only reps were corrected: keep the kg already logged (or planned), like a
                # parsed result with no load does (`training.confirm_results`).
                kept = row.actual_load_kg if row.actual_load_kg is not None else row.planned_load_kg
                fix = SetFix(fix.exercise_id, fix.set_number, fix.reps, kept)
            if exercise is not None:
                prescribed = (
                    Load(kind="kg", kg=row.planned_load_kg)
                    if row.planned_load_kg is not None and row.planned_load_kg > 0
                    else Load(kind="calibration")
                )
                verdict = check_parsed_load(
                    exercise,
                    prescribed,
                    fix.load_kg,
                    history_max_kg=history_max.get(fix.exercise_id),
                )
                verdicts.append(verdict)
                if not verdict.ok:
                    problems.append(verdict.detail)
            planned.append((fix, row))
        if problems:
            return LogEditResult(
                status=LogEditStatus.IMPLAUSIBLE, session_id=session_id, details=tuple(problems)
            )

        changes: list[SetChange] = []
        for fix, row in planned:
            await update_set_log_actual(
                conn, row.id, actual_load_kg=fix.load_kg, actual_reps=fix.reps, source=_SOURCE
            )
            changes.append(
                SetChange(
                    set_log_id=row.id,
                    exercise_id=fix.exercise_id,
                    set_number=fix.set_number,
                    before_reps=row.actual_reps,
                    before_kg=row.actual_load_kg,
                    after_reps=fix.reps,
                    after_kg=fix.load_kg,
                )
            )
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.USER_EDIT.value,
            **prompt_columns(prompt),
            content_version=content_version(),
            user_report={
                **(report_extra or {}),
                "action": FIX_ACTION,
                "session_id": session_id,
                "rows": [
                    {
                        "set_log_id": row.id,
                        "before": _row_state(row),
                        "after": {
                            "actual_reps": fix.reps,
                            "actual_load_kg": fix.load_kg,
                            "skipped": False,
                            "source": _SOURCE,
                        },
                    }
                    for fix, row in planned
                ],
            },
            proposal=None,
            guards_fired=[verdict.model_dump() for verdict in verdicts],
            load_changes=[],
        )
    return LogEditResult(
        status=LogEditStatus.OK,
        session_id=session_id,
        decision_id=decision_id,
        changes=tuple(changes),
    )


async def undo_fix(db: Database, user_id: int, decision_id: int) -> LogEditResult:
    """Restore the rows a `fix_logged_sets` decision changed — only if each row is still
    exactly as that correction left it (otherwise `STALE`, nothing written). Logged as its
    own `user_edit` decision."""
    async with db.transaction() as conn:
        decision = await get_decision(conn, decision_id)
        report = None if decision is None else decision.user_report
        if (
            decision is None
            or decision.user_id != user_id
            or report is None
            or report.get("action") != FIX_ACTION
        ):
            return LogEditResult(status=LogEditStatus.NOT_FOUND)
        session_id = report.get("session_id")
        entries = report.get("rows")
        if not isinstance(session_id, int) or not isinstance(entries, list):
            return LogEditResult(status=LogEditStatus.NOT_FOUND)
        session = await get_workout_session(conn, session_id)
        if session is None or session.user_id != user_id:
            return LogEditResult(status=LogEditStatus.NOT_FOUND)
        current = {row.id: row for row in await list_set_logs_for_session(conn, session_id)}
        for entry in entries:
            row = current.get(entry["set_log_id"])
            if row is None or _row_state(row) != entry["after"]:
                return LogEditResult(status=LogEditStatus.STALE, session_id=session_id)
        for entry in entries:
            before = entry["before"]
            await restore_set_log(
                conn,
                entry["set_log_id"],
                actual_load_kg=before["actual_load_kg"],
                actual_reps=before["actual_reps"],
                skipped=bool(before["skipped"]),
                source=before["source"],
            )
        undo_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.USER_EDIT.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report={
                "action": UNDO_ACTION,
                "session_id": session_id,
                "undoes_decision_id": decision_id,
            },
            proposal=None,
            guards_fired=[],
            load_changes=[],
        )
    return LogEditResult(status=LogEditStatus.OK, session_id=session_id, decision_id=undo_id)


__all__ = [
    "FINISHED_STATUSES",
    "LogEditResult",
    "LogEditStatus",
    "SetChange",
    "SetFix",
    "fix_logged_sets",
    "numbered_rows",
    "undo_fix",
]
