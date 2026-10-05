"""Free-text assistant (ADR 0003): the owner's unprompted message → the `assistant` agent →
typed edits applied immediately, an existing flow opened, a short reply, or a refusal.

Order of checks, for every message that reaches here (the bot's free-text handler has already
saved it and run the stop-word scan, A§6.3):

1. **Gate** (`planning.gate`): an open health hold or incomplete screening/clearance refuses
   before any model call, exactly like `/plan` and `/train`.
2. **Agent**: pseudonymized context + a small `state` + the scrubbed message; read-only
   lookup tools only (`llm.agents.AssistantTools`, implemented here by `_AssistantData`).
3. **Apply**, for `AssistantEdits`:
   - plan ops are applied to a copy of the plan by `apply_plan_ops` (pure), then saved through
     `plan_edit.save_edit` — the same guards as the website editor. Blocking failures refuse.
     The two overridable load rules (weekly cap, historical-max ceiling) also refuse here:
     there is no confirmation checkbox in chat, so an over-cap edit is only possible on the
     website's edit form, where the owner ticks it explicitly;
   - logged-set fixes go through `log_edit.fix_logged_sets` (plausibility guard).
   Every save is a `decision(kind=user_edit)` carrying `source = "assistant"`, the ops, and
   the prompt/model that interpreted them (AGENTS.md §6). Undo restores the previous state as
   another `user_edit` decision, through the same guards.

Nothing the model returns is written without passing through these functions; the model never
writes to the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.selectors.decisions import get_decision
from fitme.db.selectors.plans import get_latest_plan_version, get_plan, list_plan_versions
from fitme.db.selectors.training import (
    get_active_workout_session,
    get_workout_session,
    list_set_logs_for_session,
    list_workout_sessions_page,
)
from fitme.db.selectors.users import get_user
from fitme.domain.assistant import (
    PLAN_BODY_OPS,
    AddExercise,
    AssistantAction,
    AssistantEdits,
    AssistantReply,
    FixLoggedSet,
    PlanOp,
    RemoveExercise,
    RenamePlan,
    RenameWorkout,
    SetDefaultPlan,
    SetPrescription,
    SetSchedule,
    SwapExercise,
)
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import DecisionKind, RefusalCode
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, Workout
from fitme.i18n import wording
from fitme.llm.context import render_user_prompt, scrub
from fitme.llm.models import model_for
from fitme.llm.usage import record_llm_call, run_agent
from fitme.services import log_edit, planning
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.plan_edit import PromptMeta, prompt_columns, save_edit

AGENT_NAME = "assistant"
SOURCE = "assistant"
UNDO_SOURCE = "assistant_undo"
# Model requests per message: a few lookups plus the answer. Hitting it is a refusal.
REQUEST_LIMIT = 8


# --- Pure op application --------------------------------------------------------------------


class OpError(ValueError):
    """An op that can't be applied to the plan as it is (unknown workout/exercise, an
    exercise outside the allowed list, a schedule naming a missing workout, ...). `code` maps
    to an i18n message; `detail` is machine text (ids), safe to show."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _workout(plan: Plan, key: str) -> Workout:
    for workout in plan.workouts:
        if workout.key.casefold() == key.strip().casefold():
            return workout
    raise OpError("unknown_workout", f"workout {key!r} is not in this plan")


def _find(workout: Workout, exercise_id: str) -> tuple[Block, int]:
    for block in workout.blocks:
        for index, item in enumerate(block.items):
            if item.exercise_id == exercise_id:
                return block, index
    raise OpError("unknown_exercise", f"{exercise_id} is not in workout {workout.key}")


def _check_allowed(exercise_id: str, allowed: frozenset[str]) -> None:
    if exercise_id not in allowed:
        raise OpError("exercise_not_allowed", f"{exercise_id} is not an allowed exercise")


def _replace_item(block: Block, index: int, item: Prescription) -> None:
    block.items[index] = Prescription.model_validate(item.model_dump())


def apply_plan_ops(plan: Plan, ops: Sequence[PlanOp], allowed: frozenset[str]) -> Plan:
    """`plan` with every body op applied, as a new, re-validated `Plan` (the input is not
    modified). `RenamePlan`/`SetDefaultPlan` aren't body changes and are skipped here.
    Raises `OpError` on the first op that doesn't fit."""
    edited = plan.model_copy(deep=True)
    for op in ops:
        try:
            _apply_one(edited, op, allowed)
        except ValueError as exc:  # pydantic validation of a changed prescription
            if isinstance(exc, OpError):
                raise
            raise OpError("invalid_values", str(exc).splitlines()[0]) from exc
    try:
        return Plan.model_validate(edited.model_dump())
    except ValueError as exc:
        raise OpError("invalid_values", str(exc).splitlines()[0]) from exc


def _apply_one(plan: Plan, op: PlanOp, allowed: frozenset[str]) -> None:
    if isinstance(op, SetPrescription):
        block, index = _find(_workout(plan, op.workout_key), op.exercise_id)
        item = block.items[index].model_copy()
        if op.sets is not None:
            item.sets = op.sets
        if op.reps_min is not None:
            item.reps_min = op.reps_min
        if op.reps_max is not None:
            item.reps_max = op.reps_max
        if op.reps_min is not None and op.reps_max is None and item.reps_max < op.reps_min:
            item.reps_max = op.reps_min  # "do 10 reps" on an 6–8 range means 10–10
        if op.reps_max is not None and op.reps_min is None and item.reps_min > op.reps_max:
            item.reps_min = op.reps_max
        if op.load is not None:
            item.load = op.load
        if op.rest_seconds is not None:
            item.rest_seconds = op.rest_seconds
        _replace_item(block, index, item)
    elif isinstance(op, SwapExercise):
        _check_allowed(op.new_exercise_id, allowed)
        workout = _workout(plan, op.workout_key)
        block, index = _find(workout, op.exercise_id)
        old = block.items[index]
        _replace_item(
            block,
            index,
            old.model_copy(
                update={
                    "exercise_id": op.new_exercise_id,
                    "load": op.load if op.load is not None else Load(kind="calibration"),
                    "note": None,
                    "declared_kg": None,
                }
            ),
        )
    elif isinstance(op, AddExercise):
        _check_allowed(op.exercise_id, allowed)
        workout = _workout(plan, op.workout_key)
        item = Prescription(
            exercise_id=op.exercise_id,
            sets=op.sets,
            reps_min=op.reps_min,
            reps_max=op.reps_max,
            load=op.load,
            rest_seconds=op.rest_seconds,
        )
        workout.blocks.append(Block(kind="single", items=[item]))
    elif isinstance(op, RemoveExercise):
        workout = _workout(plan, op.workout_key)
        block, index = _find(workout, op.exercise_id)
        del block.items[index]
        if not block.items:
            workout.blocks.remove(block)
        elif len(block.items) == 1:
            block.kind = "single"
        if not workout.blocks:
            raise OpError("workout_empty", f"workout {workout.key} would have no exercises left")
    elif isinstance(op, SetSchedule):
        keys = {workout.key for workout in plan.workouts}
        weekdays = [day.weekday for day in op.days]
        if len(set(weekdays)) != len(weekdays):
            raise OpError("schedule_invalid", "the schedule names one weekday twice")
        days = []
        for day in sorted(op.days, key=lambda d: d.weekday):
            matched = next((k for k in keys if k.casefold() == day.workout_key.casefold()), None)
            if matched is None:
                raise OpError("unknown_workout", f"workout {day.workout_key!r} is not in this plan")
            days.append(day.model_copy(update={"workout_key": matched}))
        plan.schedule = days
    elif isinstance(op, RenameWorkout):
        term = wording.first_forbidden_term(op.title)
        if term is not None:
            raise OpError("forbidden_term", term)
        _workout(plan, op.workout_key).title = op.title
    # RenamePlan / SetDefaultPlan: handled by the service, not part of the body.


# --- Read-only tools for the agent ------------------------------------------------------------


def _exercise_names(exercise: Exercise | None) -> dict[str, str]:
    return {} if exercise is None else dict(exercise.names)


class _AssistantData:
    """`llm.agents.AssistantTools` over one user's data. Pseudonymized output only (AGENTS.md
    §5): ids, numbers, catalog names, scrubbed plan names."""

    def __init__(self, db: Database, user_id: int, catalog: Catalog, allowed: frozenset[str]):
        self._db = db
        self._user_id = user_id
        self._catalog = catalog
        self._allowed = allowed

    async def list_plans(self) -> list[dict[str, object]]:
        plans = await planning.list_plans(self._db, self._user_id)
        result: list[dict[str, object]] = []
        for record in plans:
            detail = await planning.get_plan_detail(self._db, self._user_id, record.id)
            workouts = (
                []
                if detail is None
                else [{"key": w.key, "title": scrub(w.title)} for w in detail.plan.workouts]
            )
            result.append(
                {
                    "plan_id": record.id,
                    "name": scrub(record.name),
                    "is_default": record.is_default,
                    "workouts": workouts,
                }
            )
        return result

    async def get_plan(self, plan_id: int) -> dict[str, object]:
        detail = await planning.get_plan_detail(self._db, self._user_id, plan_id)
        if detail is None:
            return {"error": f"no plan {plan_id}"}
        body = detail.plan.model_dump(mode="json")
        body["name"] = scrub(detail.plan.name)
        return {"plan_id": plan_id, "version": detail.version.version, "plan": body}

    async def list_recent_sessions(self, limit: int) -> list[dict[str, object]]:
        async with self._db.read() as conn:
            sessions = await list_workout_sessions_page(conn, self._user_id, limit=limit, offset=0)
        return [
            {
                "session_id": s.id,
                "workout_key": s.workout_key,
                "status": s.status,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
            }
            for s in sessions
        ]

    async def get_session(self, session_id: int) -> dict[str, object]:
        async with self._db.read() as conn:
            session = await get_workout_session(conn, session_id)
            if session is None or session.user_id != self._user_id:
                return {"error": f"no session {session_id}"}
            rows = await list_set_logs_for_session(conn, session_id)
        sets = [
            {
                "exercise_id": row.exercise_id,
                "set_number": number,
                "planned_kg": row.planned_load_kg,
                "planned_reps": [row.planned_reps_min, row.planned_reps_max],
                "actual_kg": row.actual_load_kg,
                "actual_reps": row.actual_reps,
                "skipped": row.skipped,
            }
            for number, row in log_edit.numbered_rows(rows)
        ]
        return {
            "session_id": session.id,
            "workout_key": session.workout_key,
            "status": session.status,
            "finished_at": session.finished_at,
            "editable": session.status in log_edit.FINISHED_STATUSES,
            "sets": sets,
        }

    async def find_exercises(self, query: str) -> list[dict[str, object]]:
        needle = query.strip().casefold()
        found: list[dict[str, object]] = []
        for exercise_id in sorted(self._allowed):
            exercise = self._catalog.by_id(exercise_id)
            names = _exercise_names(exercise)
            haystack = [exercise_id.replace("_", " "), exercise_id, *names.values()]
            if not needle or any(needle in text.casefold() for text in haystack):
                found.append({"exercise_id": exercise_id, "names": names})
            if len(found) >= 20:
                break
        return found


# --- Outcomes ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanEdited:
    plan_id: int
    plan_name: str
    before: Plan | None  # None when only the name/default changed
    after: Plan | None
    renamed_to: str | None = None
    made_default: bool = False
    undo_decision_id: int | None = None  # set when the plan body changed
    rename_failed: str | None = None  # an `assistant.rename_*` key; the rest was saved


@dataclass(frozen=True, slots=True)
class LogFixed:
    session_id: int
    changes: tuple[log_edit.SetChange, ...]
    undo_decision_id: int


@dataclass(frozen=True, slots=True)
class OpenFlow:
    action: str
    plan_id: int | None = None
    request: str | None = None


@dataclass(frozen=True, slots=True)
class Replied:
    text: str


@dataclass(frozen=True, slots=True)
class NotApplied:
    """Nothing changed. `key` is an `assistant.*` i18n key; `details` are machine strings
    (guard details, ids) shown under it."""

    key: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Refused:
    refusal: Refusal


Outcome = PlanEdited | LogFixed | OpenFlow | Replied | NotApplied | Refused


# --- The message handler ----------------------------------------------------------------------


async def _state(db: Database, user_id: int) -> dict[str, object]:
    plans = await planning.list_plans(db, user_id)
    async with db.read() as conn:
        user = await get_user(conn, user_id)
        active = await get_active_workout_session(conn, user_id)
    timezone = None if user is None else user.timezone
    tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    return {
        "plans": [
            {"plan_id": p.id, "name": scrub(p.name), "is_default": p.is_default} for p in plans
        ],
        "today_weekday": clock.now().astimezone(tz).weekday(),
        "workout_in_progress": active is not None,
    }


async def handle_message(db: Database, llm: LlmRuntime, user_id: int, text: str) -> Outcome:
    """One unprompted message (already stop-word-scanned by the caller, A§6.3)."""
    catalog = load_catalog()
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    lang = snapshot.language
    if snapshot.profile is None:
        return Refused(planning.refusal_for(RefusalCode.PROFILE_INCOMPLETE, lang))
    gate_failure = planning.gate(snapshot)
    if gate_failure is not None:
        code, _verdict = gate_failure
        return Refused(planning.refusal_for(code, lang))

    inputs = planning.build_inputs(catalog, snapshot, llm.settings, user_id)
    allowed = frozenset(inputs.user_context.allowed_exercise_ids)
    rendered = render_user_prompt(
        inputs.user_context, request=text, state=await _state(db, user_id)
    )
    spec = model_for(AGENT_NAME, llm.settings)
    outcome = await run_agent(
        llm.factory(AGENT_NAME),
        spec.model,
        rendered.text,
        purpose=AGENT_NAME,
        model_name=spec.model,
        prices=llm.prices,
        language=lang,
        deps=_AssistantData(db, user_id, catalog, allowed),
        request_limit=REQUEST_LIMIT,
    )
    output = outcome.output
    prompt = PromptMeta(
        template_name=outcome.prompt.template_name if outcome.prompt else AGENT_NAME,
        version=outcome.prompt.version if outcome.prompt else 0,
        model=spec.model,
        llm_input=rendered.payload,
    )

    if isinstance(output, Refusal):
        # A refusal is a decision (AGENTS.md §6: refusal is a valid output, logged like any
        # other), with the prompt/model that chose it — so "why was this refused?" can be
        # answered from the log. A provider failure (`LLM_UNAVAILABLE`) is logged the same way.
        async with db.transaction() as conn:
            decision_id = await insert_decision(
                conn,
                user_id=user_id,
                kind=DecisionKind.REFUSAL.value,
                **prompt_columns(prompt),
                content_version=content_version(),
                user_report={"source": SOURCE},
                proposal=planning.refusal_proposal(output, None),
                guards_fired=[],
            )
        await record_llm_call(db, decision_id=decision_id, record=outcome.record)
        return Refused(output)
    # Otherwise linked to no decision here: a saved edit writes its own (with the prompt and
    # model); a reply or an opened flow changes nothing.
    await record_llm_call(db, decision_id=None, record=outcome.record)
    if isinstance(output, AssistantReply):
        if wording.first_forbidden_term(output.message) is not None:
            return NotApplied("assistant.not_understood")
        return Replied(output.message)
    if isinstance(output, AssistantAction):
        return OpenFlow(action=output.action, plan_id=output.plan_id, request=output.request)
    assert isinstance(output, AssistantEdits)

    ops_json = [op.model_dump(mode="json") for op in output.ops]
    report: dict[str, object] = {"source": SOURCE, "ops": ops_json}
    fixes = [op for op in output.ops if isinstance(op, FixLoggedSet)]
    if fixes:
        return await _apply_log_fixes(db, user_id, fixes, report, prompt)
    plan_ops = [op for op in output.ops if not isinstance(op, FixLoggedSet)]
    return await _apply_plan_ops(db, llm, user_id, plan_ops, allowed, report, prompt)


async def _apply_log_fixes(
    db: Database,
    user_id: int,
    fixes: Sequence[FixLoggedSet],
    report: dict[str, object],
    prompt: PromptMeta,
) -> Outcome:
    result = await log_edit.fix_logged_sets(
        db,
        user_id,
        fixes[0].session_id,
        [log_edit.SetFix(f.exercise_id, f.set_number, f.reps, f.load_kg) for f in fixes],
        report_extra=report,
        prompt=prompt,
    )
    if result.status == log_edit.LogEditStatus.OK:
        assert result.session_id is not None and result.decision_id is not None
        return LogFixed(result.session_id, result.changes, result.decision_id)
    return NotApplied(f"assistant.log_{result.status.value}", details=result.details)


async def _apply_plan_ops(
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    ops: Sequence[PlanOp],
    allowed: frozenset[str],
    report: dict[str, object],
    prompt: PromptMeta,
) -> Outcome:
    plan_id = ops[0].plan_id
    detail = await planning.get_plan_detail(db, user_id, plan_id)
    if detail is None:
        return NotApplied("assistant.plan_not_found")
    body_ops = [op for op in ops if isinstance(op, (*PLAN_BODY_OPS, RenameWorkout))]

    before: Plan | None = None
    after: Plan | None = None
    undo_id: int | None = None
    if body_ops:
        try:
            edited = apply_plan_ops(detail.plan, body_ops, allowed)
        except OpError as exc:
            return NotApplied(f"assistant.op_{exc.code}", details=(exc.detail,))
        saved = await save_edit(
            db,
            llm.settings,
            user_id,
            plan_id,
            edited,
            confirmed=False,
            report_extra=report,
            prompt=prompt,
        )
        if saved.warnings:
            details = tuple(d for items in saved.warnings.values() for d in items)
            return NotApplied("assistant.over_cap", details=details)
        if not saved.ok:
            return NotApplied("assistant.blocked", details=tuple(saved.errors or ()))
        before, after, undo_id = detail.plan, edited, saved.decision_id

    renamed_to: str | None = None
    rename_failed: str | None = None
    made_default = False
    name = detail.record.name
    for op in ops:
        if isinstance(op, RenamePlan):
            renamed = await planning.rename_plan(db, user_id, plan_id, op.name)
            if renamed.status != planning.RenameStatus.OK:
                rename_failed = f"assistant.rename_{renamed.status.value}"
                if not body_ops:
                    return NotApplied(rename_failed)
                continue
            renamed_to = name = renamed.name or op.name
        elif isinstance(op, SetDefaultPlan):
            made_default = await planning.set_default(db, user_id, plan_id)
    return PlanEdited(
        plan_id=plan_id,
        plan_name=name,
        before=before,
        after=after,
        renamed_to=renamed_to,
        made_default=made_default,
        undo_decision_id=undo_id,
        rename_failed=rename_failed,
    )


# --- Undo -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UndoResult:
    ok: bool
    key: str  # an `assistant.undo_*` i18n key
    details: tuple[str, ...] = ()


async def undo(db: Database, llm: LlmRuntime, user_id: int, decision_id: int) -> UndoResult:
    """Revert one assistant edit. A plan edit is reverted only while its version is still
    the plan's latest (else stale), by saving the previous version's body as a new version
    — through `save_edit`'s guards like any edit. A log fix is reverted by
    `log_edit.undo_fix`."""
    async with db.read() as conn:
        decision = await get_decision(conn, decision_id)
    report = None if decision is None else decision.user_report
    if decision is None or decision.user_id != user_id or report is None:
        return UndoResult(ok=False, key="assistant.undo_stale")
    if report.get("source") != SOURCE:
        return UndoResult(ok=False, key="assistant.undo_stale")

    if report.get("action") == log_edit.FIX_ACTION:
        fixed = await log_edit.undo_fix(db, user_id, decision_id)
        ok = fixed.status == log_edit.LogEditStatus.OK
        return UndoResult(ok=ok, key="assistant.undo_done" if ok else "assistant.undo_stale")

    plan_id = report.get("plan_id")
    if not isinstance(plan_id, int):
        return UndoResult(ok=False, key="assistant.undo_stale")
    async with db.read() as conn:
        record = await get_plan(conn, plan_id)
        latest = await get_latest_plan_version(conn, plan_id)
        versions = [] if latest is None else await list_plan_versions(conn, plan_id)
    if record is None or record.user_id != user_id or latest is None:
        return UndoResult(ok=False, key="assistant.undo_stale")
    if latest.decision_id != decision_id:
        return UndoResult(ok=False, key="assistant.undo_stale")
    previous = next((v for v in versions if v.version == latest.version - 1), None)
    if previous is None:
        return UndoResult(ok=False, key="assistant.undo_stale")
    saved = await save_edit(
        db,
        llm.settings,
        user_id,
        plan_id,
        Plan.model_validate(previous.body),
        confirmed=False,
        report_extra={"source": UNDO_SOURCE, "undoes_decision_id": decision_id},
    )
    if saved.ok:
        return UndoResult(ok=True, key="assistant.undo_done")
    details = tuple(saved.errors or ()) + tuple(
        d for items in (saved.warnings or {}).values() for d in items
    )
    return UndoResult(ok=False, key="assistant.undo_blocked", details=details)


__all__ = [
    "LogFixed",
    "NotApplied",
    "OpError",
    "OpenFlow",
    "Outcome",
    "PlanEdited",
    "Refused",
    "Replied",
    "UndoResult",
    "apply_plan_ops",
    "handle_message",
    "undo",
]
