"""Free-text assistant (ADR 0003, ADR 0004): the owner's message, with the open session's
recent turns, → the `assistant` agent and its tools → what it staged, applied through the
guards → what to show.

Order of checks for every message that reaches here (the bot's free-text handler has already
saved it and run the stop-word scan, A§6.3):

1. **Gate** (`planning.gate`): an open health hold or incomplete screening/clearance refuses
   before any model call, exactly like `/plan` and `/train`.
2. **Agent**: pseudonymized context, a small `state` (plans, the open session, its draft or
   the workout's current block), the session's last turns and the scrubbed message.
3. **Tools** (`_Turn`, the `llm.agents.AssistantTools` implementation): lookups read; staging
   tools check an action against the current state right away — the op applies, the guards
   accept it — and record it, so the model can correct itself within the same turn.
4. **Apply** what was staged, after the run, through the same code as the buttons: a plan
   edit becomes a draft round of the planning session (`planning.record_edit_draft`, judged
   like any draft; "Save" is `planning.confirm_plan`); a change of today's workout is
   `training.edit_remaining`; a logged-set fix is `log_edit.fix_logged_sets` (with Undo).
   Redesigns, new plans, block logging and screens are handed to the bot's existing flows.

The model never writes to the database; every write goes through the functions above.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.records import ConversationRecord, PlanRecord
from fitme.db.selectors.decisions import get_decision
from fitme.db.selectors.plans import get_latest_plan_version, get_plan, list_plan_versions
from fitme.db.selectors.training import (
    get_workout_session,
    list_set_logs_for_session,
    list_workout_sessions_page,
)
from fitme.db.selectors.users import get_user
from fitme.domain.assistant import (
    AddExercise,
    AnyPlanOp,
    AnyWorkoutOp,
    AssistantTurn,
    FixLoggedSet,
    RemoveExercise,
    RenameWorkout,
    SetPrescription,
    SetSchedule,
    SwapExercise,
)
from fitme.domain.catalog import Catalog
from fitme.domain.enums import DecisionKind, RefusalCode, WorkoutSessionStatus
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, Workout
from fitme.i18n import wording
from fitme.llm.context import render_user_prompt, scrub
from fitme.llm.models import model_for
from fitme.llm.usage import record_llm_call, run_agent
from fitme.services import conversations, log_edit, planning, training
from fitme.services.decisions import PromptMeta, prompt_columns
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.plan_edit import save_edit

AGENT_NAME = "assistant"
SOURCE = "assistant"
UNDO_SOURCE = "assistant_undo"
# Model requests per message: lookups, a few staged actions with corrections, the answer.
# Hitting it is a refusal (`LLM_UNAVAILABLE`), never a partial write.
REQUEST_LIMIT = 15


# --- Pure op application --------------------------------------------------------------------


class OpError(ValueError):
    """An op that can't be applied as things are (unknown workout/exercise, an exercise
    outside the allowed list, a block already done, ...). `detail` is machine text (ids),
    returned to the model and safe to show."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _workout(plan: Plan, key: str | None) -> Workout:
    if key is None:
        if len(plan.workouts) == 1:
            return plan.workouts[0]
        raise OpError("unknown_workout", "workout_key is required: this plan has several")
    for workout in plan.workouts:
        if workout.key.casefold() == key.strip().casefold():
            return workout
    raise OpError("unknown_workout", f"workout {key!r} is not in this plan")


def _find(workout: Workout, exercise_id: str, *, first_block: int = 0) -> tuple[Block, int]:
    for block in workout.blocks[first_block:]:
        for index, item in enumerate(block.items):
            if item.exercise_id == exercise_id:
                return block, index
    done = (item for block in workout.blocks[:first_block] for item in block.items)
    if any(item.exercise_id == exercise_id for item in done):
        raise OpError("block_started", f"{exercise_id} is in a block already started or done")
    raise OpError("unknown_exercise", f"{exercise_id} is not in workout {workout.key}")


def _check_allowed(exercise_id: str, allowed: frozenset[str]) -> None:
    if exercise_id not in allowed:
        raise OpError("exercise_not_allowed", f"{exercise_id} is not an allowed exercise")


def _replace_item(block: Block, index: int, item: Prescription) -> None:
    block.items[index] = Prescription.model_validate(item.model_dump())


def _apply_to_workout(
    workout: Workout, op: AnyWorkoutOp, allowed: frozenset[str], *, first_block: int = 0
) -> None:
    if isinstance(op, SetPrescription):
        block, index = _find(workout, op.exercise_id, first_block=first_block)
        item = block.items[index].model_copy()
        if op.sets is not None:
            item.sets = op.sets
        if op.reps_min is not None:
            item.reps_min = op.reps_min
        if op.reps_max is not None:
            item.reps_max = op.reps_max
        if op.reps_min is not None and op.reps_max is None and item.reps_max < op.reps_min:
            item.reps_max = op.reps_min  # "do 10 reps" on a 6–8 range means 10–10
        if op.reps_max is not None and op.reps_min is None and item.reps_min > op.reps_max:
            item.reps_min = op.reps_max
        if op.load is not None:
            item.load = op.load
        if op.rest_seconds is not None:
            item.rest_seconds = op.rest_seconds
        _replace_item(block, index, item)
    elif isinstance(op, SwapExercise):
        _check_allowed(op.new_exercise_id, allowed)
        block, index = _find(workout, op.exercise_id, first_block=first_block)
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
        block, index = _find(workout, op.exercise_id, first_block=first_block)
        del block.items[index]
        if not block.items:
            workout.blocks.remove(block)
        elif len(block.items) == 1:
            block.kind = "single"
        if not workout.blocks:
            raise OpError("workout_empty", f"workout {workout.key} would have no exercises left")


def _apply_schedule(plan: Plan, op: SetSchedule) -> None:
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


def apply_plan_ops(plan: Plan, ops: Sequence[AnyPlanOp], allowed: frozenset[str]) -> Plan:
    """`plan` with every op applied, as a new, re-validated `Plan` (the input is not
    modified). Raises `OpError` on the first op that doesn't fit."""
    edited = plan.model_copy(deep=True)
    for op in ops:
        try:
            if isinstance(op, SetSchedule):
                _apply_schedule(edited, op)
            elif isinstance(op, RenameWorkout):
                term = wording.first_forbidden_term(op.title)
                if term is not None:
                    raise OpError("forbidden_term", term)
                _workout(edited, op.workout_key).title = op.title
            else:
                _apply_to_workout(_workout(edited, op.workout_key), op, allowed)
        except OpError:
            raise
        except ValueError as exc:  # pydantic validation of a changed prescription
            raise OpError("invalid_values", str(exc).splitlines()[0]) from exc
    try:
        return Plan.model_validate(edited.model_dump())
    except ValueError as exc:
        raise OpError("invalid_values", str(exc).splitlines()[0]) from exc


def apply_workout_ops(
    workout: Workout, ops: Sequence[AnyWorkoutOp], allowed: frozenset[str], *, first_block: int
) -> Workout:
    """Today's `workout` with every op applied to blocks from `first_block` on (earlier ones
    are started or done). A new, re-validated `Workout`; raises `OpError`."""
    edited = workout.model_copy(deep=True)
    for op in ops:
        try:
            _apply_to_workout(edited, op, allowed, first_block=first_block)
        except OpError:
            raise
        except ValueError as exc:
            raise OpError("invalid_values", str(exc).splitlines()[0]) from exc
    try:
        return Workout.model_validate(edited.model_dump())
    except ValueError as exc:
        raise OpError("invalid_values", str(exc).splitlines()[0]) from exc


# --- What a turn shows afterwards -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftUpdated:
    """The planning session's draft after this turn's edits (shown as a diff, Save / Close)."""

    result: planning.EditDraftResult
    before: Plan | None


@dataclass(frozen=True, slots=True)
class TodayChanged:
    before: Workout
    result: training.EditRemainingResult


@dataclass(frozen=True, slots=True)
class LogFixed:
    session_id: int
    changes: tuple[log_edit.SetChange, ...]
    undo_decision_id: int


@dataclass(frozen=True, slots=True)
class Notice:
    """A one-line result: `key` is an `assistant.*` i18n key."""

    key: str
    name: str = ""  # the only parameter a notice has (a plan name)
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DraftSaved:
    confirm: planning.ConfirmResult


@dataclass(frozen=True, slots=True)
class RunRewrite:
    base: planning.RevisionBase
    request: str


@dataclass(frozen=True, slots=True)
class RunNewPlan:
    guidance: str | None


@dataclass(frozen=True, slots=True)
class RunLogBlock:
    skip: bool


@dataclass(frozen=True, slots=True)
class RunEnterResults:
    pass


@dataclass(frozen=True, slots=True)
class RunShow:
    view: str  # "plans" | "plan" | "draft" | "stats" | "train"
    plan_id: int | None = None
    draft_decision_id: int | None = None


Item = (
    DraftUpdated
    | TodayChanged
    | LogFixed
    | Notice
    | DraftSaved
    | RunRewrite
    | RunNewPlan
    | RunLogBlock
    | RunEnterResults
    | RunShow
)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """`message` (the assistant's words, wording-checked) is shown first, then `items` in
    order. `refusal` replaces both."""

    message: str | None = None
    items: tuple[Item, ...] = ()
    refusal: Refusal | None = None


@dataclass
class _Staged:
    """What a run staged, applied in `_apply` after it."""

    draft_plan: Plan | None = None  # the planning draft as edited this turn
    draft_before: Plan | None = None
    draft_plan_id: int | None = None
    today: Workout | None = None
    today_before: Workout | None = None
    today_session_id: int | None = None
    fixes: tuple[int, list[FixLoggedSet]] | None = None
    renames: list[tuple[int, str]] = field(default_factory=list)
    default_plan_id: int | None = None
    rewrite: RunRewrite | None = None
    new_plan: RunNewPlan | None = None
    save: bool = False
    discard: bool = False
    block: RunLogBlock | RunEnterResults | None = None
    shows: list[RunShow] = field(default_factory=list)


def _ok(**extra: object) -> dict[str, object]:
    return {"ok": True, **extra}


def _error(message: str) -> dict[str, object]:
    return {"ok": False, "error": message}


def _items_of(workout: Workout) -> list[Prescription]:
    return [item for block in workout.blocks for item in block.items]


def _load_str(load: Load) -> str:
    return f"{load.kg:g} kg" if load.kind == "kg" and load.kg is not None else load.kind


def _load_notes_workout(requested: Workout, judged: Workout) -> list[str]:
    return [
        f"{asked.exercise_id}: {_load_str(asked.load)} is above what the safety rules allow "
        f"now; it will be {_load_str(got.load)}"
        for asked, got in zip(_items_of(requested), _items_of(judged), strict=True)
        if asked.load != got.load
    ]


def _load_notes(requested: Plan, judged: Plan) -> list[str]:
    notes: list[str] = []
    for asked, got in zip(requested.workouts, judged.workouts, strict=True):
        notes.extend(_load_notes_workout(asked, got))
    return notes


class _Turn:
    """`llm.agents.AssistantTools` for one message. Pseudonymized output only (AGENTS.md
    §5): ids, numbers, catalog names, scrubbed plan names."""

    def __init__(
        self,
        *,
        db: Database,
        user_id: int,
        catalog: Catalog,
        inputs: planning.Inputs,
        planning_session: ConversationRecord | None,
        workout: training.ActiveSession | None,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._catalog = catalog
        self._inputs = inputs
        self._allowed = frozenset(inputs.user_context.allowed_exercise_ids)
        self._planning = planning_session
        self._workout = workout
        self.staged = _Staged()

    async def _draft_is_current(self, record: ConversationRecord | None) -> bool:
        if record is None or record.draft_decision_id is None:
            return False
        return await planning.current_draft_id(self._db, self._user_id) == record.draft_decision_id

    async def _plan_record(self, plan_id: int) -> PlanRecord | None:
        async with self._db.read() as conn:
            record = await get_plan(conn, plan_id)
        return record if record is not None and record.user_id == self._user_id else None

    # --- lookups

    async def list_plans(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for record in await planning.list_plans(self._db, self._user_id):
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
            return _error(f"no plan {plan_id}")
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
                return _error(f"no session {session_id}")
            rows = await list_set_logs_for_session(conn, session_id)
        return {
            "session_id": session.id,
            "workout_key": session.workout_key,
            "status": session.status,
            "finished_at": session.finished_at,
            "editable": session.status in log_edit.FINISHED_STATUSES,
            "sets": [
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
            ],
        }

    async def find_exercises(self, query: str) -> list[dict[str, object]]:
        needle = query.strip().casefold()
        found: list[dict[str, object]] = []
        for exercise_id in sorted(self._allowed):
            exercise = self._catalog.by_id(exercise_id)
            names = {} if exercise is None else dict(exercise.names)
            haystack = [exercise_id.replace("_", " "), exercise_id, *names.values()]
            if not needle or any(needle in text.casefold() for text in haystack):
                found.append({"exercise_id": exercise_id, "names": names})
            if len(found) >= 20:
                break
        return found

    # --- planning session

    async def edit_plan(self, plan_id: int | None, ops: list[AnyPlanOp]) -> dict[str, object]:
        staged = self.staged
        if staged.rewrite is not None or staged.new_plan is not None:
            return _error("a redesign or a new plan is already staged this turn")
        session = self._planning
        if plan_id is None:
            if session is None:
                return _error("no planning session is open: pass the plan_id to change")
            plan_id = session.plan_id
        elif await self._plan_record(plan_id) is None:
            return _error(f"no plan {plan_id}")
        if (
            session is not None
            and session.plan_id != plan_id
            and await self._draft_is_current(session)
        ):
            return _error(
                "another plan's draft is open in the planning session: ask the user to save "
                "or discard it first"
            )
        if staged.draft_plan is not None and staged.draft_plan_id == plan_id:
            base = staged.draft_plan
        else:
            draft_id = (
                session.draft_decision_id
                if session is not None and session.plan_id == plan_id
                else None
            )
            found = await planning.draft_base_plan(
                self._db, self._user_id, plan_id=plan_id, draft_decision_id=draft_id
            )
            if found is None:
                return _error("there is no draft or saved plan to change yet")
            base = found
            staged.draft_before = found
        try:
            edited = apply_plan_ops(base, ops, self._allowed)
        except OpError as exc:
            return _error(exc.detail)
        preview = edited.model_copy(deep=True)
        judgement = planning.judge(preview, self._inputs)
        if not judgement.ok:
            return _error("; ".join(verdict.detail for verdict in judgement.failures))
        staged.draft_plan = edited
        staged.draft_plan_id = plan_id
        return _ok(
            draft=preview.model_dump(mode="json"),
            notes=_load_notes(edited, preview),
            saved=False,
            hint="a draft: the user saves it with the Save button or by asking you",
        )

    async def rewrite_plan(self, plan_id: int | None, request: str) -> dict[str, object]:
        staged = self.staged
        if staged.draft_plan is not None or staged.new_plan is not None:
            return _error("specific edits or a new plan are already staged this turn")
        if staged.rewrite is not None:
            return _error("a redesign is already staged this turn")
        if not request.strip():
            return _error("request is empty")
        session = self._planning
        target = plan_id if plan_id is not None else (None if session is None else session.plan_id)
        base: planning.RevisionBase
        if (
            session is not None
            and session.plan_id == target
            and await self._draft_is_current(session)
        ):
            assert session.draft_decision_id is not None
            base = planning.DraftBase(session.draft_decision_id)
        elif target is not None:
            if await self._plan_record(target) is None:
                return _error(f"no plan {target}")
            base = planning.PlanBase(target)
        else:
            return _error("which plan? pass plan_id, or use new_plan for a new one")
        staged.rewrite = RunRewrite(base=base, request=request.strip())
        return _ok(staged="the redesigned draft is shown after this turn")

    async def new_plan(self, guidance: str | None) -> dict[str, object]:
        staged = self.staged
        if staged.draft_plan is not None or staged.rewrite is not None:
            return _error("other plan changes are already staged this turn")
        session = self._planning
        if (
            session is not None
            and session.plan_id is not None
            and await self._draft_is_current(session)
        ):
            return _error(
                "a draft of another plan is open: ask the user to save or discard it first"
            )
        staged.new_plan = RunNewPlan(guidance=(guidance or "").strip() or None)
        return _ok(staged="the new plan's draft is generated and shown after this turn")

    async def save_draft(self) -> dict[str, object]:
        if self.staged.rewrite is not None or self.staged.new_plan is not None:
            return _error("the redesign isn't ready yet: the user saves it once they've seen it")
        if self.staged.draft_plan is None and not await self._draft_is_current(self._planning):
            return _error("there is no draft to save")
        self.staged.save = True
        return _ok(staged="saved after this turn")

    async def discard_draft(self) -> dict[str, object]:
        if self._planning is None and self.staged.draft_plan is None:
            return _error("no planning session is open")
        self.staged.discard = True
        self.staged.draft_plan = None
        return _ok(staged="the session is closed without saving after this turn")

    async def rename_plan(self, plan_id: int, name: str) -> dict[str, object]:
        if await self._plan_record(plan_id) is None:
            return _error(f"no plan {plan_id}")
        if wording.first_forbidden_term(name) is not None:
            return _error("that name uses wording this app doesn't use")
        self.staged.renames.append((plan_id, name))
        return _ok()

    async def set_default_plan(self, plan_id: int) -> dict[str, object]:
        if await self._plan_record(plan_id) is None:
            return _error(f"no plan {plan_id}")
        self.staged.default_plan_id = plan_id
        return _ok()

    # --- training log

    async def fix_logged_sets(
        self, session_id: int, fixes: list[FixLoggedSet]
    ) -> dict[str, object]:
        if not fixes:
            return _error("no fixes given")
        if self.staged.fixes is not None and self.staged.fixes[0] != session_id:
            return _error("one session's sets per turn")
        async with self._db.read() as conn:
            session = await get_workout_session(conn, session_id)
            rows = [] if session is None else await list_set_logs_for_session(conn, session_id)
        if session is None or session.user_id != self._user_id:
            return _error(f"no session {session_id}")
        if session.status not in log_edit.FINISHED_STATUSES:
            return _error("this session isn't finished: results go through the workout itself")
        existing = {(row.exercise_id, number) for number, row in log_edit.numbered_rows(rows)}
        missing = [f for f in fixes if (f.exercise_id, f.set_number) not in existing]
        if missing:
            return _error(
                "no such set: " + ", ".join(f"{f.exercise_id} set {f.set_number}" for f in missing)
            )
        previous = [] if self.staged.fixes is None else self.staged.fixes[1]
        self.staged.fixes = (session_id, [*previous, *fixes])
        return _ok()

    # --- training session

    def _open_block(self) -> tuple[int, int] | None:
        active = self._workout
        if active is None or active.session.status != WorkoutSessionStatus.IN_PROGRESS.value:
            return None
        return active.session.id, active.session.current_block

    async def log_current_block(self, skip: bool) -> dict[str, object]:
        if self._open_block() is None:
            return _error("no workout block is open")
        if self.staged.block is not None:
            return _error("one block action per turn")
        self.staged.block = RunLogBlock(skip=skip)
        return _ok()

    async def enter_current_block_results(self) -> dict[str, object]:
        if self._open_block() is None:
            return _error("no workout block is open")
        if self.staged.block is not None:
            return _error("one block action per turn")
        self.staged.block = RunEnterResults()
        return _ok(staged="the user's message goes to the result parser after this turn")

    async def edit_today(self, ops: list[AnyWorkoutOp]) -> dict[str, object]:
        open_block = self._open_block()
        active = self._workout
        if open_block is None or active is None:
            return _error("no workout is in progress")
        session_id, current = open_block
        base = self.staged.today if self.staged.today is not None else active.workout
        try:
            edited = apply_workout_ops(base, ops, self._allowed, first_block=current + 1)
        except OpError as exc:
            return _error(exc.detail)
        preview = edited.model_copy(deep=True)
        judgement = planning.judge_workout(preview, self._inputs)
        if not judgement.ok:
            return _error("; ".join(verdict.detail for verdict in judgement.failures))
        if self.staged.today is None:
            self.staged.today_before = active.workout
        self.staged.today = edited
        self.staged.today_session_id = session_id
        return _ok(
            remaining_blocks=[
                block.model_dump(mode="json") for block in preview.blocks[current + 1 :]
            ],
            notes=_load_notes_workout(edited, preview),
        )

    # --- screens

    async def show(self, view: str, plan_id: int | None) -> dict[str, object]:
        if view not in ("plans", "plan", "draft", "stats", "train"):
            return _error(f"unknown view {view!r}")
        if view == "plan" and plan_id is None:
            return _error("which plan? pass plan_id")
        draft_id = None
        if view == "draft":
            if not await self._draft_is_current(self._planning):
                return _error("there is no draft")
            assert self._planning is not None
            draft_id = self._planning.draft_decision_id
        self.staged.shows.append(RunShow(view=view, plan_id=plan_id, draft_decision_id=draft_id))
        return _ok()


# --- The message handler ----------------------------------------------------------------------


def _current_block(active: training.ActiveSession | None) -> dict[str, object] | None:
    """The block on screen of an `in_progress` workout and the blocks after it: ids and
    numbers only. `None` before Start (precheck/review) or with no active session."""
    if active is None or active.session.status != WorkoutSessionStatus.IN_PROGRESS.value:
        return None
    index = active.session.current_block
    if not 0 <= index < active.total_blocks:
        return None
    block = active.workout.blocks[index]
    return {
        "block": index + 1,
        "of": active.total_blocks,
        "exercises": [
            {
                "exercise_id": item.exercise_id,
                "sets": item.sets,
                "reps": [item.reps_min, item.reps_max],
                "load": item.load.model_dump(mode="json"),
            }
            for item in block.items
        ],
        "later_blocks": [
            later.model_dump(mode="json") for later in active.workout.blocks[index + 1 :]
        ],
    }


async def _state(
    db: Database,
    user_id: int,
    *,
    conversation: ConversationRecord | None,
    planning_session: ConversationRecord | None,
    active: training.ActiveSession | None,
) -> dict[str, object]:
    plans = await planning.list_plans(db, user_id)
    async with db.read() as conn:
        user = await get_user(conn, user_id)
    timezone = None if user is None else user.timezone
    tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    state: dict[str, object] = {
        "plans": [
            {"plan_id": p.id, "name": scrub(p.name), "is_default": p.is_default} for p in plans
        ],
        "today_weekday": clock.now().astimezone(tz).weekday(),
        "workout_in_progress": active is not None,
        "session": None if conversation is None else conversation.kind,
    }
    current = _current_block(active)
    if current is not None:
        state["current_block"] = current
    if planning_session is not None:
        session: dict[str, object] = {"plan_id": planning_session.plan_id, "draft": None}
        draft_id = planning_session.draft_decision_id
        if draft_id is not None and await planning.current_draft_id(db, user_id) == draft_id:
            draft = await planning.draft_base_plan(
                db, user_id, plan_id=None, draft_decision_id=draft_id
            )
            if draft is not None:
                body = draft.model_dump(mode="json")
                body["name"] = scrub(draft.name)
                session["draft"] = body
        state["planning_session"] = session
    return state


async def handle_message(
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    *,
    chat_message_id: int | None = None,
) -> TurnResult:
    """One message no button or pending prompt claimed (already stop-word-scanned by the
    caller, A§6.3)."""
    catalog = load_catalog()
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    lang = snapshot.language
    if snapshot.profile is None:
        return TurnResult(refusal=planning.refusal_for(RefusalCode.PROFILE_INCOMPLETE, lang))
    gate_failure = planning.gate(snapshot)
    if gate_failure is not None:
        return TurnResult(refusal=planning.refusal_for(gate_failure[0], lang))

    conversation = await conversations.active(db, user_id)
    planning_session = await conversations.current_planning(db, user_id)
    turns = (
        []
        if conversation is None
        else await conversations.history(db, conversation.id, exclude_message_id=chat_message_id)
    )
    workout = await training.active_session(db, user_id)

    inputs = planning.build_inputs(catalog, snapshot, llm.settings, user_id)
    state = await _state(
        db,
        user_id,
        conversation=conversation,
        planning_session=planning_session,
        active=workout,
    )
    rendered = render_user_prompt(
        inputs.user_context,
        request=text,
        state=state,
        history=[(turn.role, turn.text) for turn in turns],
    )
    turn = _Turn(
        db=db,
        user_id=user_id,
        catalog=catalog,
        inputs=inputs,
        planning_session=planning_session,
        workout=workout,
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
        deps=turn,
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
        # A refusal is a decision (AGENTS.md §6), with the prompt/model that chose it. Nothing
        # staged is applied: a refused (or failed) run changes nothing.
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
        return TurnResult(refusal=output)
    # A saved edit writes its own decision (with the prompt and model); the call itself is
    # linked to none.
    await record_llm_call(db, decision_id=None, record=outcome.record)
    assert isinstance(output, AssistantTurn)

    items = await _apply(db, llm.settings, user_id, turn, prompt=prompt)
    message = output.message.strip()
    if wording.first_forbidden_term(message) is not None:
        message = ""
    # Where the exchange continues: a turn that opened a planning session belongs to it.
    ended_in = conversation
    if conversation is None or conversation.kind == conversations.PLANNING:
        ended_in = await conversations.current_planning(db, user_id) or conversation
    if ended_in is not None:
        moved = conversation is None or ended_in.id != conversation.id
        if chat_message_id is not None and moved:
            await conversations.link_incoming(db, chat_message_id, ended_in.id)
        await conversations.record_outgoing(db, user_id, ended_in.id, message)
    return TurnResult(message=message or None, items=tuple(items))


async def _apply(
    db: Database, settings: Settings, user_id: int, turn: _Turn, *, prompt: PromptMeta
) -> list[Item]:
    """Apply what the run staged, in a fixed order: draft edits, today's workout, log fixes,
    renames, the default, save / discard; then the flows the bot runs itself."""
    staged = turn.staged
    items: list[Item] = []
    report: dict[str, object] = {"source": SOURCE}

    if staged.draft_plan is not None:
        plan_id = staged.draft_plan_id
        result = await planning.record_edit_draft(
            db,
            settings,
            user_id,
            plan=staged.draft_plan,
            plan_id=plan_id,
            user_report={**report, "base": "assistant_edit"},
            prompt=prompt,
        )
        if result.round is not None:
            await conversations.planning_draft_shown(
                db, user_id, plan_id=plan_id, draft_decision_id=result.round.decision_id
            )
        items.append(DraftUpdated(result=result, before=staged.draft_before))

    if staged.today is not None and staged.today_before is not None:
        assert staged.today_session_id is not None
        changed = await training.edit_remaining(
            db,
            settings,
            user_id,
            staged.today_session_id,
            staged.today,
            prompt=prompt,
            report_extra=report,
        )
        items.append(TodayChanged(before=staged.today_before, result=changed))

    if staged.fixes is not None:
        session_id, fixes = staged.fixes
        fixed = await log_edit.fix_logged_sets(
            db,
            user_id,
            session_id,
            [log_edit.SetFix(f.exercise_id, f.set_number, f.reps, f.load_kg) for f in fixes],
            report_extra=report,
            prompt=prompt,
        )
        if fixed.status == log_edit.LogEditStatus.OK:
            assert fixed.session_id is not None and fixed.decision_id is not None
            items.append(LogFixed(fixed.session_id, fixed.changes, fixed.decision_id))
        else:
            items.append(Notice(f"assistant.log_{fixed.status.value}", details=fixed.details))

    for plan_id, name in staged.renames:
        renamed = await planning.rename_plan(db, user_id, plan_id, name)
        if renamed.status == planning.RenameStatus.OK:
            items.append(Notice("assistant.renamed", name=renamed.name or name))
        else:
            items.append(Notice(f"assistant.rename_{renamed.status.value}"))

    if staged.default_plan_id is not None and await planning.set_default(
        db, user_id, staged.default_plan_id
    ):
        detail = await planning.get_plan_detail(db, user_id, staged.default_plan_id)
        if detail is not None:
            items.append(Notice("assistant.made_default", name=detail.record.name))

    if staged.save:
        session = await conversations.current_planning(db, user_id)
        draft_id = None if session is None else session.draft_decision_id
        if draft_id is None:
            items.append(Notice("assistant.nothing_to_save"))
        else:
            confirmed = await planning.confirm_plan(db, settings, user_id, draft_id)
            if confirmed.status in (
                planning.ConfirmStatus.SAVED,
                planning.ConfirmStatus.ALREADY_SAVED,
            ):
                await conversations.close_planning(db, user_id, status="saved")
            items.append(DraftSaved(confirmed))
    elif staged.discard:
        await conversations.close_planning(db, user_id, status="discarded")
        items.append(Notice("assistant.draft_discarded"))

    if staged.rewrite is not None:
        items.append(staged.rewrite)
    if staged.new_plan is not None:
        items.append(staged.new_plan)
    if staged.block is not None:
        items.append(staged.block)
    items.extend(staged.shows)
    return items


# --- Undo -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UndoResult:
    ok: bool
    key: str  # an `assistant.undo_*` i18n key
    details: tuple[str, ...] = ()


async def undo(db: Database, llm: LlmRuntime, user_id: int, decision_id: int) -> UndoResult:
    """Revert one assistant edit that was applied directly: a logged-set fix
    (`log_edit.undo_fix`), or — for buttons sent before ADR 0004, when plan edits were applied
    at once — a plan edit, only while its version is still the plan's latest, by saving the
    previous version's body through `save_edit`'s guards."""
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
    "DraftSaved",
    "DraftUpdated",
    "Item",
    "LogFixed",
    "Notice",
    "OpError",
    "RunEnterResults",
    "RunLogBlock",
    "RunNewPlan",
    "RunRewrite",
    "RunShow",
    "TodayChanged",
    "TurnResult",
    "UndoResult",
    "apply_plan_ops",
    "apply_workout_ops",
    "handle_message",
    "undo",
]
