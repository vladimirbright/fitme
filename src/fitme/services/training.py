"""The `/train` workout loop (A§6.5), UI-agnostic. The state machine lives in
`workout_sessions.status` + `current_block`, so a restart resumes exactly where it stopped
(A§3): nothing here depends on in-memory state.

```
select_plan ─► select_workout ─► precheck ─► review ⇄ adjust ─► in_progress ─► completed
   (no row)       (no row)       [draft]      [confirmed]        [in_progress]   │ aborted
                                     └─► halted                        └─► halted
```

- **No row** until a workout is chosen: `entry()` picks the default/only plan and today's
  workout (in the user's timezone; else the next in rotation after the last completed
  session on that plan) — the UI offers "pick another" — and `create_session()` writes the
  `draft` row.
- **`draft`** = the precheck is pending (A§6.5 step 3). Only an explicit **No** moves on
  (`precheck_no` → `confirmed`); **Yes** halts (`precheck_yes`); anything else leaves the
  session where it is (AGENTS.md §2: silence is not consent).
- **`confirmed`** = review (step 4). The workout shown carries the **load engine's** loads
  (A§7.3, `planning.engine_load` with the full `GuardContext`), never the stored plan numbers
  alone. `adjust()` sends free text (already stop-word-scanned by the caller) to the
  `session_adjust` agent through `run_with_escalation`; the result is judged by the same
  `planning.judge` as a plan (load violations get the engine's value, structural ones retry,
  then refuse). Every attempt is a `session_adjust` decision with `event="adjust"`, its
  `llm_input` verbatim and `load_changes = []` — a draft applies nothing. The current draft
  is the newest such decision, else the engine workout recomputed now.
- **Start** (`start()`) re-validates the draft against a fresh `GuardContext` in ONE
  transaction and writes the *applying* record: a `session_adjust` decision with
  `event="start"`, holding the exact workout that will be trained and its `load_changes`
  (every kg above the A§7 reference, incl. the applied-this-week lift), so the weekly cap
  sees a session-only increase exactly once (A§4.3 "load changes count once, when
  applied"). `save_to_plan()` instead writes a new plan version through the same
  re-validating `plan_confirm` pattern as `/plan` (its `load_changes` then lift the reference
  at Start, so nothing is counted twice).
- **`in_progress`**: one block at a time. Sending block `i` creates its `set_logs` rows —
  one per prescribed set, planned load/reps filled, `actual_*` NULL — in the same
  transaction that advances `current_block`, so a resume finds the rows and never
  duplicates them (`assign_rows` maps existing rows back onto blocks in creation order).
  "According to plan" writes actual = planned; "Skip" marks the block's sets `skipped=1`;
  "Enter results" goes stop-word scan → `result_parse` (per prescription, so `set_index`
  is unambiguous even in a superset) → `safety_signal` halts → `unclear` re-asks →
  `guards.plausibility` re-asks → shown for confirmation; only a confirmed parse writes
  `actual_*`.
- **Halts** (A§6.6) all go through `services.safety.halt`, which halts the active session
  and ties the hold to it. **Abort** keeps the sets as logged.

Unit-of-work rules (A§4.6): read → close → LLM → transaction → write; never a network await
inside a unit. Every state transition checks the session's owner, status and block, so a
stale or double-tapped button is answered with a status, not applied twice.

M8 hook: `_complete()` is where the recap, the check-ins and the progression decision go.
Note for M8's progression: after an engine *decrease*, `GuardContext.applied_to_kg_7d`
(the highest `to_kg` applied this week, e.g. by an earlier `start` decision) must not lift
the reference back to a load the user just failed at within the same week; the engine's
"hold at the load applied this week" branch needs to yield to a decrease.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Connection, Database
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome
from fitme.db.controllers.plans import insert_plan_version
from fitme.db.controllers.training import (
    finish_workout_session,
    insert_checkin,
    insert_set_log,
    insert_workout_session,
    mark_set_log_skipped,
    start_workout_session,
    update_set_log_actual,
    update_workout_session_progress,
)
from fitme.db.records import (
    DecisionRecord,
    PlanRecord,
    PlanVersionRecord,
    SetLogRecord,
    WorkoutSessionRecord,
)
from fitme.db.selectors.decisions import (
    get_latest_session_event_decision,
    list_decision_outcomes,
)
from fitme.db.selectors.plans import (
    get_default_plan,
    get_latest_plan_version,
    get_plan,
    get_plan_version,
    list_plans_for_user,
)
from fitme.db.selectors.profile import list_screening_flags
from fitme.db.selectors.training import (
    get_active_workout_session,
    get_workout_session,
    last_completed_workout_key_for_plan,
    list_checkins_for_session,
    list_set_logs_for_session,
)
from fitme.db.selectors.users import get_user
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import (
    DecisionKind,
    HealthHoldReason,
    RefusalCode,
    ScreeningFlag,
    WorkoutSessionStatus,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, LoadChange, Plan, Prescription, Refusal, Workout
from fitme.domain.results import ParsedResults, SetResult
from fitme.domain.screening import ScreeningFlagState
from fitme.guards import stop_words
from fitme.guards.checkins import flagged_areas_from
from fitme.guards.layering import combine_with_llm_signal
from fitme.guards.plausibility import check_parsed_results
from fitme.llm.context import render_user_prompt
from fitme.llm.escalation import run_with_escalation
from fitme.llm.models import model_for
from fitme.llm.usage import AgentRunOutcome, record_llm_call, run_agent
from fitme.services import planning
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.safety import HaltResult, halt

_PLAN_STATUS_ACTIVE = "active"
_ORIGIN_LLM = "llm"
_SOURCE_BUTTON = "button"
_SOURCE_FREE_TEXT = "free_text"
_EVENT_ADJUST = "adjust"  # a guard-accepted adjustment: can be the current draft
_EVENT_ADJUST_REJECTED = "adjust_rejected"  # a rejected attempt or a refusal: logged, never shown
EVENT_START = "start"
_EVENT_PARSE = "parse"
_PARSE_SHOWN = "shown"
_ENGINE_RULE = "loads.engine"


# --- Result types -----------------------------------------------------------------------------


class Status(StrEnum):
    OK = "ok"
    STALE = "stale"  # the session isn't in the state this action needs (old button, double tap)
    NOT_FOUND = "not_found"  # no such session for this user
    REFUSED = "refused"  # a gate or guard refused; `refusal` is set
    ALREADY = "already"  # idempotent repeat: nothing new happened
    STARTED = "started"  # the session is already in progress (or finished): too late for this


@dataclass(frozen=True, slots=True)
class BlockView:
    """One block as the bot shows it (A§6.5 step 5), with the loads that were fixed at Start."""

    session_id: int
    index: int  # 0-based
    total: int
    workout_key: str
    title: str
    block: Block


@dataclass(frozen=True, slots=True)
class ReviewView:
    """The whole workout with explicit loads (A§6.5 step 4)."""

    session_id: int
    workout: Workout
    plan_name: str
    adjusted: bool  # an LLM adjustment is the current draft (offers "Save to plan")


@dataclass(frozen=True, slots=True)
class Refused:
    refusal: Refusal
    decision_id: int | None = None


@dataclass(frozen=True, slots=True)
class NoPlan:
    pass


@dataclass(frozen=True, slots=True)
class ChoosePlan:
    plans: list[PlanRecord]


@dataclass(frozen=True, slots=True)
class WorkoutSuggested:
    plan: PlanRecord
    workout: Workout
    scheduled_today: bool
    others: list[Workout]  # the plan's other workouts, for "pick another"


@dataclass(frozen=True, slots=True)
class ActiveSession:
    session: WorkoutSessionRecord
    workout: Workout
    plan_name: str

    @property
    def total_blocks(self) -> int:
        return len(self.workout.blocks)


Entry = Refused | NoPlan | ChoosePlan | WorkoutSuggested | ActiveSession


@dataclass(frozen=True, slots=True)
class SessionCreated:
    session: WorkoutSessionRecord
    workout: Workout


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    status: Status
    review: ReviewView | None = None
    halt: HaltResult | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True, slots=True)
class AdjustResult:
    status: Status
    review: ReviewView | None = None
    refusal: Refusal | None = None
    halt: HaltResult | None = None  # a stop word in the request text: the halt path ran


@dataclass(frozen=True, slots=True)
class SaveResult:
    status: Status
    plan_name: str | None = None
    version: int | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True, slots=True)
class StartResult:
    status: Status
    block: BlockView | None = None
    refusal: Refusal | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    sets_logged: int
    sets_planned: int


@dataclass(frozen=True, slots=True)
class Advance:
    """After a block was handled: the next block to send, or the completed summary."""

    status: Status
    next_block: BlockView | None = None
    completion: Completion | None = None


@dataclass(frozen=True, slots=True)
class ResultPrompt:
    """Which prescription to ask results for next (per item, so a superset is entered one
    exercise at a time and `SetResult.set_index` is always per prescription)."""

    session_id: int
    block: int
    item: int
    prescription: Prescription
    exercise: Exercise | None


class ParseStatus(StrEnum):
    SHOWN = "shown"  # a plausible parse; show it for Correct / Fix
    UNCLEAR = "unclear"  # the model couldn't parse it: ask again
    IMPLAUSIBLE = "implausible"  # `guards.plausibility` rejected it: ask again, store nothing
    HALTED = "halted"  # a stop word or the model's safety signal: the halt path ran
    UNAVAILABLE = "unavailable"  # provider failure (A§8.5 rule 4): a refusal, nothing stored
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ParseResult:
    status: ParseStatus
    prompt: ResultPrompt | None = None
    parsed: ParsedResults | None = None
    # `None` with `HALTED` when the session had already been halted (e.g. by ⚠) before the
    # model's safety signal came back: no second hold is opened.
    halt: HaltResult | None = None
    refusal: Refusal | None = None
    decision_id: int | None = None


@dataclass(frozen=True, slots=True)
class ConfirmResults:
    status: Status
    next_prompt: ResultPrompt | None = None  # more items in this block to enter
    advance: Advance | None = None  # the block is complete


# --- Session context (one read) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionContext:
    session: WorkoutSessionRecord
    plan_record: PlanRecord
    version: PlanVersionRecord
    plan: Plan
    workout: Workout  # the plan's stored workout for this session's key


async def load_session(conn: Connection, user_id: int, session_id: int) -> SessionContext | None:
    session = await get_workout_session(conn, session_id)
    if session is None or session.user_id != user_id:
        return None
    version = await get_plan_version(conn, session.plan_version_id)
    if version is None:
        return None
    plan_record = await get_plan(conn, version.plan_id)
    if plan_record is None:
        return None
    plan = Plan.model_validate(version.body)
    workout = _workout_by_key(plan, session.workout_key)
    if workout is None:
        return None
    return SessionContext(
        session=session, plan_record=plan_record, version=version, plan=plan, workout=workout
    )


def _workout_by_key(plan: Plan, key: str) -> Workout | None:
    return next((workout for workout in plan.workouts if workout.key == key), None)


def _decision_workout(decision: DecisionRecord | None) -> Workout | None:
    if decision is None or decision.proposal is None:
        return None
    body = decision.proposal.get("workout")
    if body is None:
        return None
    try:
        return Workout.model_validate(body)
    except ValueError:
        return None


async def _draft_workout(conn: Connection, session_id: int) -> tuple[Workout | None, int | None]:
    """The newest LLM adjustment for this session (A§6.5 step 4), if any, and its decision id."""
    decision = await get_latest_session_event_decision(
        conn, session_id=session_id, kind=DecisionKind.SESSION_ADJUST.value, event=_EVENT_ADJUST
    )
    workout = _decision_workout(decision)
    return workout, None if workout is None or decision is None else decision.id


async def started_workout(conn: Connection, session_id: int) -> Workout | None:
    """The workout fixed at Start (the applying `session_adjust`/`start` decision)."""
    decision = await get_latest_session_event_decision(
        conn, session_id=session_id, kind=DecisionKind.SESSION_ADJUST.value, event=EVENT_START
    )
    return _decision_workout(decision)


# --- Engine loads and judging over one workout -----------------------------------------------


def _engine_workout(
    workout: Workout, inputs: planning.Inputs
) -> tuple[Workout, list[GuardVerdict]]:
    """A copy of `workout` with every prescription's load replaced by the load engine's value
    (A§6.5 step 4: "the loads come from the load engine, not stored plan numbers alone")."""
    copy = workout.model_copy(deep=True)
    fired: list[GuardVerdict] = []
    for block in copy.blocks:
        for prescription in block.items:
            exercise = inputs.catalog.by_id(prescription.exercise_id)
            if exercise is None:
                continue  # a non-catalog id fails `judge` structurally; nothing to compute
            load, reason, verdicts = planning.engine_load(exercise, inputs)
            prescription.load = load
            fired.append(
                GuardVerdict(rule=_ENGINE_RULE, ok=True, detail=f"{exercise.id}: {reason}")
            )
            fired.extend(verdicts)
    return copy, fired


def _plan_with(plan: Plan, workout: Workout) -> Plan:
    """`plan` with the workout of the same key replaced by `workout` (the same object, so an
    in-place load substitution by `planning.judge` lands on it)."""
    return Plan(
        name=plan.name,
        schedule=list(plan.schedule),
        workouts=[workout if item.key == workout.key else item for item in plan.workouts],
    )


def _judge_workout(workout: Workout, inputs: planning.Inputs) -> planning.Judgement:
    """A§6.5.1: the gate plus per-prescription verdicts over today's workout (with load
    substitution), not the plan-wide schedule rules."""
    return planning.judge_workout(workout, inputs)


def _workout_load_changes(workout: Workout, inputs: planning.Inputs) -> list[LoadChange]:
    """A§4.3: the kg loads of this one workout above the reference, for the applying decision."""
    return planning.load_changes_for(
        Plan(name=workout.title, schedule=[], workouts=[workout]), inputs.ctx
    )


# --- Entry (/train) ---------------------------------------------------------------------------


async def _write_refusal(
    conn: Connection,
    *,
    user_id: int,
    refusal: Refusal,
    verdict: GuardVerdict,
    session_id: int | None,
) -> int:
    report: dict[str, object] = {"flow": "train"}
    if session_id is not None:
        report["session_id"] = session_id
    return await planning.write_refusal_decision(
        conn, user_id=user_id, refusal=refusal, guards_fired=[verdict], user_report=report
    )


async def _gate_refusal(
    db: Database, user_id: int, session_id: int | None = None
) -> Refused | None:
    """A§6.5: an open hold or incomplete screening refuses before the precheck (the same
    gates as `/plan`), logged as `decision(kind=refusal)`."""
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    failure = planning.gate(snapshot)
    if failure is None:
        return None
    code, verdict = failure
    refusal = planning.refusal_for(code, snapshot.language)
    async with db.transaction() as conn:
        decision_id = await _write_refusal(
            conn, user_id=user_id, refusal=refusal, verdict=verdict, session_id=session_id
        )
    return Refused(refusal=refusal, decision_id=decision_id)


def _today_weekday(timezone: str | None) -> int:
    tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    return clock.now().astimezone(tz).weekday()


def _pick_workout(
    plan: Plan, *, weekday: int, last_completed_key: str | None
) -> tuple[Workout, bool]:
    """A§6.5 step 2: today's scheduled workout, else the next in rotation after the last
    completed one (the first workout when nothing was completed yet or the key is gone)."""
    scheduled = next((day.workout_key for day in plan.schedule if day.weekday == weekday), None)
    if scheduled is not None:
        workout = _workout_by_key(plan, scheduled)
        if workout is not None:
            return workout, True
    keys = [workout.key for workout in plan.workouts]
    if last_completed_key in keys:
        index = (keys.index(last_completed_key) + 1) % len(keys)
        return plan.workouts[index], False
    return plan.workouts[0], False


async def suggest_workout(db: Database, user_id: int, plan_id: int) -> WorkoutSuggested | None:
    """Today's workout of `plan_id` (A§6.5 steps 1-2), or `None` for a plan that isn't the
    user's, isn't active or has no workouts."""
    async with db.read() as conn:
        user = await get_user(conn, user_id)
        record = await get_plan(conn, plan_id)
        if user is None or record is None or record.user_id != user_id:
            return None
        if record.status != _PLAN_STATUS_ACTIVE:
            return None
        version = await get_latest_plan_version(conn, plan_id)
        if version is None:
            return None
        last_key = await last_completed_workout_key_for_plan(conn, user_id, plan_id)
    plan = Plan.model_validate(version.body)
    if not plan.workouts:
        return None
    workout, today = _pick_workout(
        plan, weekday=_today_weekday(user.timezone), last_completed_key=last_key
    )
    others = [item for item in plan.workouts if item.key != workout.key]
    return WorkoutSuggested(plan=record, workout=workout, scheduled_today=today, others=others)


async def active_session(db: Database, user_id: int) -> ActiveSession | None:
    async with db.read() as conn:
        session = await get_active_workout_session(conn, user_id)
        if session is None:
            return None
        ctx = await load_session(conn, user_id, session.id)
        if ctx is None:
            return None
        started = await started_workout(conn, session.id)
    workout = started if started is not None else ctx.workout
    return ActiveSession(session=session, workout=workout, plan_name=ctx.plan_record.name)


async def entry(db: Database, user_id: int) -> Entry:
    """`/train` (A§6.5 steps 1-2): gates first, then an active session to continue, then the
    plan (default, only, or a choice) and the workout to suggest."""
    refused = await _gate_refusal(db, user_id)
    if refused is not None:
        return refused
    active = await active_session(db, user_id)
    if active is not None:
        return active
    async with db.read() as conn:
        default = await get_default_plan(conn, user_id)
        plans = [
            plan
            for plan in await list_plans_for_user(conn, user_id)
            if plan.status == _PLAN_STATUS_ACTIVE
        ]
    if not plans:
        return NoPlan()
    chosen: PlanRecord | None = None
    if default is not None and default.status == _PLAN_STATUS_ACTIVE:
        chosen = default
    elif len(plans) == 1:
        chosen = plans[0]
    if chosen is None:
        return ChoosePlan(plans=plans)
    suggested = await suggest_workout(db, user_id, chosen.id)
    return suggested if suggested is not None else NoPlan()


async def create_session(
    db: Database, user_id: int, plan_id: int, workout_key: str
) -> SessionCreated | ActiveSession | Refused | None:
    """Write the `draft` row for the chosen workout (the precheck follows). An `in_progress`
    session is returned instead (resume it or abort it first); an unstarted `draft`/
    `confirmed` one is aborted, since the user just chose a different workout. `None` for a
    plan/workout that isn't available."""
    refused = await _gate_refusal(db, user_id)
    if refused is not None:
        return refused
    active = await active_session(db, user_id)
    if active is not None and active.session.status == WorkoutSessionStatus.IN_PROGRESS.value:
        return active
    async with db.transaction() as conn:
        record = await get_plan(conn, plan_id)
        if record is None or record.user_id != user_id or record.status != _PLAN_STATUS_ACTIVE:
            return None
        version = await get_latest_plan_version(conn, plan_id)
        if version is None:
            return None
        workout = _workout_by_key(Plan.model_validate(version.body), workout_key)
        if workout is None:
            return None
        previous = await get_active_workout_session(conn, user_id)
        if previous is not None:
            await finish_workout_session(
                conn, previous.id, status=WorkoutSessionStatus.ABORTED.value
            )
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version.id,
            workout_key=workout_key,
            status=WorkoutSessionStatus.DRAFT.value,
        )
        session = await get_workout_session(conn, session_id)
    assert session is not None
    return SessionCreated(session=session, workout=workout)


# --- Precheck and review ----------------------------------------------------------------------


async def _build_review(
    db: Database, settings: Settings, user_id: int, session_id: int
) -> ReviewView | Refused | None:
    """The review workout: the newest LLM adjustment if there is one, else the plan's workout
    with the engine's loads, computed from a fresh snapshot (A§6.5 step 4)."""
    async with db.read() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return None
        snapshot = await planning.read_snapshot(conn, user_id)
        draft, _draft_id = await _draft_workout(conn, session_id)
    failure = planning.gate(snapshot)
    if failure is not None:
        code, verdict = failure
        refusal = planning.refusal_for(code, snapshot.language)
        async with db.transaction() as conn:
            decision_id = await _write_refusal(
                conn, user_id=user_id, refusal=refusal, verdict=verdict, session_id=session_id
            )
        return Refused(refusal=refusal, decision_id=decision_id)
    inputs = planning.build_inputs(load_catalog(), snapshot, settings, user_id)
    if draft is not None:
        return ReviewView(
            session_id=session_id, workout=draft, plan_name=ctx.plan_record.name, adjusted=True
        )
    workout, _fired = _engine_workout(ctx.workout, inputs)
    return ReviewView(
        session_id=session_id, workout=workout, plan_name=ctx.plan_record.name, adjusted=False
    )


async def precheck_no(
    db: Database, settings: Settings, user_id: int, session_id: int
) -> PrecheckResult:
    """The explicit **No** (A§6.5 step 3): `draft` → `confirmed`, then the review."""
    async with db.transaction() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return PrecheckResult(status=Status.NOT_FOUND)
        if ctx.session.status != WorkoutSessionStatus.DRAFT.value:
            return PrecheckResult(status=Status.STALE)
        await update_workout_session_progress(
            conn, session_id, status=WorkoutSessionStatus.CONFIRMED.value, current_block=0
        )
    review = await _build_review(db, settings, user_id, session_id)
    if isinstance(review, Refused):
        return PrecheckResult(status=Status.REFUSED, refusal=review.refusal)
    if review is None:
        return PrecheckResult(status=Status.NOT_FOUND)
    return PrecheckResult(status=Status.OK, review=review)


async def precheck_yes(db: Database, user_id: int, session_id: int) -> PrecheckResult:
    """**Yes** → the halt path (A§6.6), tied to this session. A stale button still halts:
    the user just reported a symptom."""
    async with db.read() as conn:
        ctx = await load_session(conn, user_id, session_id)
        active = await get_active_workout_session(conn, user_id)
    if ctx is None and (active is None or active.id != session_id):
        # A forged or unknown id: a safe no-op. The owner's own active session halts even
        # when it can't be loaded in full, consistent with the ⚠ button.
        return PrecheckResult(status=Status.NOT_FOUND)
    result = await halt(
        db,
        user_id=user_id,
        reason=HealthHoldReason.PRECHECK_YES,
        guards_fired=[
            GuardVerdict(rule="precheck.answer", ok=False, detail="precheck answered yes")
        ],
        user_report={"trigger": "precheck_yes", "event": "halt"},
    )
    return PrecheckResult(status=Status.OK, halt=result)


async def review(db: Database, settings: Settings, user_id: int, session_id: int) -> PrecheckResult:
    """Re-show the review of a `confirmed` session (after an adjustment, or on resume)."""
    async with db.read() as conn:
        ctx = await load_session(conn, user_id, session_id)
    if ctx is None:
        return PrecheckResult(status=Status.NOT_FOUND)
    if ctx.session.status != WorkoutSessionStatus.CONFIRMED.value:
        return PrecheckResult(status=Status.STALE)
    built = await _build_review(db, settings, user_id, session_id)
    if isinstance(built, Refused):
        return PrecheckResult(status=Status.REFUSED, refusal=built.refusal)
    if built is None:
        return PrecheckResult(status=Status.NOT_FOUND)
    return PrecheckResult(status=Status.OK, review=built)


# --- Adjust (LLM) -----------------------------------------------------------------------------


async def _log_adjust_attempt(
    db: Database,
    *,
    user_id: int,
    session_id: int,
    outcome: AgentRunOutcome[Workout | Refusal],
    llm_input: dict[str, object] | None,
    judgement: planning.Judgement | None,
    inputs: planning.Inputs,
) -> int:
    """One `session_adjust` decision per attempt: `llm_input` verbatim, the judged workout
    (or the model's refusal) as the proposal, `load_changes = []`. A§6.5.1: only a
    guard-accepted attempt is `event="adjust"` (and so can become the current draft); a
    rejected attempt or a refusal is `event="adjust_rejected"`, so the last accepted draft
    stays in effect."""
    output = outcome.output
    event = _EVENT_ADJUST_REJECTED
    if isinstance(output, Refusal):
        proposal = planning.refusal_proposal(output, cause=outcome.record.error_cause)
        fired: list[GuardVerdict] = []
    else:
        assert judgement is not None
        fired = judgement.fired
        proposed = _workout_load_changes(output, inputs) if judgement.ok else []
        proposal = {
            "workout": output.model_dump(mode="json"),
            "proposed_load_changes": [change.model_dump(mode="json") for change in proposed],
        }
        if judgement.ok:
            event = _EVENT_ADJUST
    prompt = outcome.prompt
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.SESSION_ADJUST.value,
            prompt_template=None if prompt is None else prompt.template_name,
            prompt_version=None if prompt is None else str(prompt.version),
            model=outcome.record.model,
            content_version=content_version(),
            llm_input=llm_input,
            user_report={"session_id": session_id, "event": event},
            proposal=proposal,
            guards_fired=[verdict.model_dump() for verdict in fired],
            load_changes=[],
        )


async def adjust(
    db: Database, llm: LlmRuntime, user_id: int, session_id: int, request_text: str
) -> AdjustResult:
    """A§6.5 step 4 "Adjust": the stop-word scan of the user's text **before any LLM call**
    (the bot scans free text first too, A§6.3; this is defense in depth — a hit halts and the
    model is never invoked) → `session_adjust` with escalation (A§8.5 rule 3) →
    `planning.judge_workout` → the new draft, or a refusal (the last accepted draft stays)."""
    async with db.read() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return AdjustResult(status=Status.NOT_FOUND)
        if ctx.session.status != WorkoutSessionStatus.CONFIRMED.value:
            return AdjustResult(status=Status.STALE)
        snapshot = await planning.read_snapshot(conn, user_id)
        draft, _draft_id = await _draft_workout(conn, session_id)
    lang = snapshot.language
    hit = stop_words.scan(request_text, lang)
    if hit is not None:
        halted = await halt(
            db,
            user_id=user_id,
            reason=HealthHoldReason.STOP_WORD,
            guards_fired=[stop_words.to_verdict(hit)],
            user_report={"trigger": "stop_word", "event": "halt", "category": hit.category},
        )
        return AdjustResult(status=Status.REFUSED, halt=halted)
    failure = planning.gate(snapshot)
    if failure is not None:
        code, verdict = failure
        refusal = planning.refusal_for(code, lang)
        async with db.transaction() as conn:
            await _write_refusal(
                conn, user_id=user_id, refusal=refusal, verdict=verdict, session_id=session_id
            )
        return AdjustResult(status=Status.REFUSED, refusal=refusal)
    inputs = planning.build_inputs(load_catalog(), snapshot, llm.settings, user_id)
    current = draft if draft is not None else _engine_workout(ctx.workout, inputs)[0]

    payload_by_text: dict[str, dict[str, object]] = {}
    judgements: dict[int, planning.Judgement] = {}
    # M8b: the declared hints come from the plan's stored workout, never from the model.
    plan_declared = planning.declared_loads_of(_plan_with(ctx.plan, ctx.workout))

    def build_prompt(verdicts: Sequence[GuardVerdict] | None) -> str:
        feedback = (
            None if verdicts is None else [verdict.detail for verdict in verdicts if not verdict.ok]
        )
        rendered = render_user_prompt(
            inputs.user_context, request=request_text, workout=current, guard_feedback=feedback
        )
        payload_by_text[rendered.text] = rendered.payload
        return rendered.text

    def guard_check(workout: Workout) -> Sequence[GuardVerdict]:
        # The adjustment is for this session's workout, whatever key the model wrote.
        workout.key = ctx.workout.key
        planning.restore_declared(_plan_with(ctx.plan, workout), plan_declared)
        judgement = _judge_workout(workout, inputs)
        judgements[id(workout)] = judgement
        return judgement.verdicts

    async def on_attempt(
        outcome: AgentRunOutcome[Workout | Refusal], _verdicts: Sequence[GuardVerdict]
    ) -> int:
        prompt_text = None if outcome.prompt is None else outcome.prompt.user_prompt
        return await _log_adjust_attempt(
            db,
            user_id=user_id,
            session_id=session_id,
            outcome=outcome,
            llm_input=None if prompt_text is None else payload_by_text.get(prompt_text),
            judgement=judgements.get(id(outcome.output)),
            inputs=inputs,
        )

    escalation = await run_with_escalation(
        agent_name="session_adjust",
        agent_factory=llm.factory("session_adjust"),
        build_prompt=build_prompt,
        guard_check=guard_check,
        settings=llm.settings,
        db=db,
        prices=llm.prices,
        language=lang,
        on_attempt=on_attempt,
    )
    if isinstance(escalation.output, Refusal):
        return AdjustResult(status=Status.REFUSED, refusal=escalation.output)
    return AdjustResult(
        status=Status.OK,
        review=ReviewView(
            session_id=session_id,
            workout=escalation.output,
            plan_name=ctx.plan_record.name,
            adjusted=True,
        ),
    )


# --- Save to plan -----------------------------------------------------------------------------


async def save_to_plan(
    db: Database, settings: Settings, user_id: int, session_id: int
) -> SaveResult:
    """A§6.5 step 4 "Save to plan": the current adjustment becomes version n+1 of the
    session's plan, through the same re-validating `plan_confirm` pattern as `/plan` (A§4.3:
    the confirm decision carries the `load_changes`). Idempotent via the adjust decision's
    `decision_outcomes`."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return SaveResult(status=Status.NOT_FOUND)
        if ctx.session.status in (
            WorkoutSessionStatus.IN_PROGRESS.value,
            WorkoutSessionStatus.COMPLETED.value,
        ):
            return SaveResult(status=Status.STARTED)
        if ctx.session.status != WorkoutSessionStatus.CONFIRMED.value:
            return SaveResult(status=Status.STALE)
        draft, draft_id = await _draft_workout(conn, session_id)
        if draft is None or draft_id is None:
            return SaveResult(status=Status.STALE)  # nothing adjusted: nothing to save
        for outcome in await list_decision_outcomes(conn, draft_id):
            version_number = outcome.outcome.get("version")
            if isinstance(version_number, int):
                return SaveResult(
                    status=Status.ALREADY, plan_name=ctx.plan_record.name, version=version_number
                )
        snapshot = await planning.read_snapshot(conn, user_id)
        lang = snapshot.language
        failure = planning.gate(snapshot)
        if failure is not None:
            code, verdict = failure
            refusal = planning.refusal_for(code, lang)
            await _write_refusal(
                conn, user_id=user_id, refusal=refusal, verdict=verdict, session_id=session_id
            )
            return SaveResult(status=Status.REFUSED, refusal=refusal)
        inputs = planning.build_inputs(catalog, snapshot, settings, user_id)
        latest = await get_latest_plan_version(conn, ctx.plan_record.id)
        base = ctx.plan if latest is None else Plan.model_validate(latest.body)
        new_plan = _plan_with(base, draft)
        judgement = _judge_workout(draft, inputs)
        if not judgement.ok:
            refusal = planning.refusal_for(RefusalCode.NO_SAFE_WORKOUT, lang)
            await planning.write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=judgement.failures,
                user_report={"flow": "train", "session_id": session_id, "save_of": draft_id},
                rejected_plan=new_plan,
            )
            return SaveResult(status=Status.REFUSED, refusal=refusal)
        version = 1 if latest is None else latest.version + 1
        load_changes = planning.load_changes_for(new_plan, inputs.ctx)
        confirm_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.PLAN_CONFIRM.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report={
                "draft_decision_id": draft_id,
                "plan_id": ctx.plan_record.id,
                "session_id": session_id,
                "flow": "train",
            },
            proposal=planning.draft_proposal(new_plan, load_changes),
            guards_fired=[verdict.model_dump() for verdict in judgement.fired],
            load_changes=load_changes,
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=ctx.plan_record.id,
            version=version,
            body=new_plan.model_dump(mode="json"),
            origin=_ORIGIN_LLM,
            decision_id=confirm_id,
        )
        outcome_body: dict[str, object] = {
            "confirmed": True,
            "plan_id": ctx.plan_record.id,
            "plan_version_id": version_id,
            "version": version,
            "confirm_decision_id": confirm_id,
        }
        await insert_decision_outcome(conn, decision_id=draft_id, outcome=outcome_body)
        await insert_decision_outcome(conn, decision_id=confirm_id, outcome=outcome_body)
    return SaveResult(status=Status.OK, plan_name=ctx.plan_record.name, version=version)


# --- Set rows ---------------------------------------------------------------------------------


def assign_rows(rows: Sequence[SetLogRecord], workout: Workout) -> list[list[list[SetLogRecord]]]:
    """Map a session's `set_logs` rows (creation order) back onto `workout`'s blocks and
    items: `[block][item] -> rows`. Rows are handed out per exercise in order, so an exercise
    that appears in two blocks gets its first rows on the earlier block. A block whose items
    don't all have `sets` rows hasn't been sent yet (rows are created per block, atomically).
    """
    queues: dict[str, list[SetLogRecord]] = {}
    for row in rows:
        queues.setdefault(row.exercise_id, []).append(row)
    assigned: list[list[list[SetLogRecord]]] = []
    for block in workout.blocks:
        per_item: list[list[SetLogRecord]] = []
        for item in block.items:
            queue = queues.get(item.exercise_id, [])
            taken = queue[: item.sets]
            del queue[: item.sets]
            per_item.append(taken)
        assigned.append(per_item)
    return assigned


def _block_rows_exist(
    assigned: list[list[list[SetLogRecord]]], workout: Workout, index: int
) -> bool:
    return all(
        len(rows) == item.sets
        for item, rows in zip(workout.blocks[index].items, assigned[index], strict=True)
    )


async def _ensure_block_rows(
    conn: Connection, session_id: int, workout: Workout, index: int
) -> None:
    """Create block `index`'s rows if they aren't there yet (A§4.2: one per prescribed set,
    created when the block is sent; a resume must not duplicate them)."""
    rows = await list_set_logs_for_session(conn, session_id)
    assigned = assign_rows(rows, workout)
    if _block_rows_exist(assigned, workout, index):
        return
    for item in workout.blocks[index].items:
        for set_index in range(1, item.sets + 1):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=item.exercise_id,
                set_index=set_index,
                planned_load_kg=item.load.kg,
                planned_reps_min=item.reps_min,
                planned_reps_max=item.reps_max,
                actual_load_kg=None,
                actual_reps=None,
                rpe=None,
                source=_SOURCE_BUTTON,
            )


def _block_view(session_id: int, workout: Workout, index: int) -> BlockView:
    return BlockView(
        session_id=session_id,
        index=index,
        total=len(workout.blocks),
        workout_key=workout.key,
        title=workout.title,
        block=workout.blocks[index],
    )


def is_logged(row: SetLogRecord) -> bool:
    return row.skipped or row.actual_reps is not None


# --- Start and in-progress ------------------------------------------------------------------


async def start(db: Database, settings: Settings, user_id: int, session_id: int) -> StartResult:
    """A§6.5 "Start", in ONE transaction: re-validate the current draft against a fresh
    `GuardContext` (load violations get the engine's value; a structural failure refuses),
    write the applying `session_adjust`/`start` decision with its `load_changes` (A§4.3),
    move to `in_progress` and create block 0's rows. A double tap on an already started
    session returns `ALREADY` with the current block."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return StartResult(status=Status.NOT_FOUND)
        if ctx.session.status == WorkoutSessionStatus.IN_PROGRESS.value:
            started = await started_workout(conn, session_id)
            if started is None:
                return StartResult(status=Status.STALE)
            await _ensure_block_rows(conn, session_id, started, ctx.session.current_block)
            return StartResult(
                status=Status.ALREADY,
                block=_block_view(session_id, started, ctx.session.current_block),
            )
        if ctx.session.status != WorkoutSessionStatus.CONFIRMED.value:
            return StartResult(status=Status.STALE)
        snapshot = await planning.read_snapshot(conn, user_id)
        lang = snapshot.language
        failure = planning.gate(snapshot)
        if failure is not None:
            code, verdict = failure
            refusal = planning.refusal_for(code, lang)
            await _write_refusal(
                conn, user_id=user_id, refusal=refusal, verdict=verdict, session_id=session_id
            )
            return StartResult(status=Status.REFUSED, refusal=refusal)
        inputs = planning.build_inputs(catalog, snapshot, settings, user_id)
        draft, draft_id = await _draft_workout(conn, session_id)
        fired: list[GuardVerdict] = []
        if draft is None:
            workout, fired = _engine_workout(ctx.workout, inputs)
        else:
            workout = draft
        judgement = _judge_workout(workout, inputs)
        fired.extend(judgement.fired)
        if not judgement.ok:
            refusal = planning.refusal_for(RefusalCode.NO_SAFE_WORKOUT, lang)
            await planning.write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=judgement.failures,
                user_report={"flow": "train", "session_id": session_id, "event": "start"},
            )
            return StartResult(status=Status.REFUSED, refusal=refusal)
        if not workout.blocks:
            return StartResult(status=Status.STALE)
        load_changes = _workout_load_changes(workout, inputs)
        report: dict[str, object] = {
            "session_id": session_id,
            "event": EVENT_START,
            "adjusted": draft is not None,
        }
        if draft_id is not None:
            report["draft_decision_id"] = draft_id
        await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.SESSION_ADJUST.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=report,
            proposal={
                "workout": workout.model_dump(mode="json"),
                "load_changes": [change.model_dump(mode="json") for change in load_changes],
            },
            guards_fired=[verdict.model_dump() for verdict in fired],
            load_changes=load_changes,
        )
        await update_workout_session_progress(
            conn, session_id, status=WorkoutSessionStatus.IN_PROGRESS.value, current_block=0
        )
        await start_workout_session(conn, session_id)
        await _ensure_block_rows(conn, session_id, workout, 0)
    return StartResult(status=Status.OK, block=_block_view(session_id, workout, 0))


@dataclass(frozen=True, slots=True)
class _Progress:
    ctx: SessionContext
    workout: Workout  # the started workout
    assigned: list[list[list[SetLogRecord]]]
    rows: list[SetLogRecord]


async def _load_progress(
    conn: Connection, user_id: int, session_id: int, *, block: int | None
) -> tuple[Status, _Progress | None]:
    """The in-progress session, its started workout and its rows, or why not: `NOT_FOUND`,
    or `STALE` when the session isn't `in_progress` or `block` isn't the current block."""
    ctx = await load_session(conn, user_id, session_id)
    if ctx is None:
        return Status.NOT_FOUND, None
    if ctx.session.status != WorkoutSessionStatus.IN_PROGRESS.value:
        return Status.STALE, None
    if block is not None and block != ctx.session.current_block:
        return Status.STALE, None
    workout = await started_workout(conn, session_id)
    if workout is None or ctx.session.current_block >= len(workout.blocks):
        return Status.STALE, None
    rows = await list_set_logs_for_session(conn, session_id)
    return Status.OK, _Progress(
        ctx=ctx, workout=workout, assigned=assign_rows(rows, workout), rows=rows
    )


async def current_block(db: Database, user_id: int, session_id: int) -> StartResult:
    """Resume (A§6.5 step 2, A§3): the current block of the `in_progress` session, with its
    rows created if a crash happened before they were."""
    async with db.transaction() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=None)
        if progress is None:
            return StartResult(status=status)
        index = progress.ctx.session.current_block
        await _ensure_block_rows(conn, session_id, progress.workout, index)
    return StartResult(status=Status.OK, block=_block_view(session_id, progress.workout, index))


def _completion(rows: Sequence[SetLogRecord]) -> Completion:
    return Completion(
        sets_logged=sum(1 for row in rows if row.actual_reps is not None),
        sets_planned=len(rows),
    )


async def _complete(conn: Connection, user_id: int, session_id: int) -> Completion:
    """The last block is done, in the same transaction (A§6.5 step 6, M8): `completed`;
    one `checkins` row (`unknown`) per **flagged** area that today's exercises load, so the
    next increase for that area is blocked until an explicit answer (silence is not
    consent); and the `start` decision's `decision_outcomes` row with planned vs actual per
    set (AGENTS.md §6). The recap text (`services.recap`) follows outside the transaction."""
    await finish_workout_session(conn, session_id, status=WorkoutSessionStatus.COMPLETED.value)
    rows = await list_set_logs_for_session(conn, session_id)
    workout = await started_workout(conn, session_id)
    if workout is not None and not await list_checkins_for_session(conn, session_id):
        flags = [
            ScreeningFlagState(
                flag=ScreeningFlag(record.flag), value=record.value, clearance=record.clearance
            )
            for record in await list_screening_flags(conn, user_id)
        ]
        for area in loaded_flagged_areas(workout, load_catalog(), flagged_areas_from(flags)):
            await insert_checkin(
                conn, user_id=user_id, session_id=session_id, question_key=f"area:{area}"
            )
    start_decision = await get_latest_session_event_decision(
        conn, session_id=session_id, kind=DecisionKind.SESSION_ADJUST.value, event=EVENT_START
    )
    if start_decision is not None:
        await insert_decision_outcome(
            conn,
            decision_id=start_decision.id,
            outcome={"session_id": session_id, "completed": True, "sets": set_outcomes(rows)},
        )
    return _completion(rows)


def loaded_flagged_areas(workout: Workout, catalog: Catalog, flagged: frozenset[str]) -> list[str]:
    """The flagged areas (`guards.checkins.flagged_areas_from`) that `workout`'s exercises
    load (catalog `loads_areas`), in workout order: the check-ins to ask after it."""
    areas: list[str] = []
    for block in workout.blocks:
        for item in block.items:
            exercise = catalog.by_id(item.exercise_id)
            if exercise is None:
                continue
            for area in exercise.loads_areas:
                if area in flagged and area not in areas:
                    areas.append(area)
    return areas


def set_outcomes(rows: Sequence[SetLogRecord]) -> list[dict[str, object]]:
    """Planned vs actual per set, as `decision_outcomes.outcome["sets"]` stores it."""
    return [
        {
            "exercise_id": row.exercise_id,
            "set_index": row.set_index,
            "planned_load_kg": row.planned_load_kg,
            "planned_reps_min": row.planned_reps_min,
            "planned_reps_max": row.planned_reps_max,
            "actual_load_kg": row.actual_load_kg,
            "actual_reps": row.actual_reps,
            "skipped": row.skipped,
        }
        for row in rows
    ]


async def _advance(conn: Connection, progress: _Progress) -> Advance:
    session_id = progress.ctx.session.id
    next_index = progress.ctx.session.current_block + 1
    if next_index >= len(progress.workout.blocks):
        completion = await _complete(conn, progress.ctx.session.user_id, session_id)
        return Advance(status=Status.OK, completion=completion)
    await update_workout_session_progress(
        conn, session_id, status=WorkoutSessionStatus.IN_PROGRESS.value, current_block=next_index
    )
    await _ensure_block_rows(conn, session_id, progress.workout, next_index)
    return Advance(
        status=Status.OK, next_block=_block_view(session_id, progress.workout, next_index)
    )


async def complete_block_as_planned(
    db: Database, user_id: int, session_id: int, block: int
) -> Advance:
    """✅ According to plan: actual = planned for every not-yet-logged set of the block. The
    reps logged are the top of the prescribed range (A§7.3 double progression: "according to
    plan" means every set reached `reps_max`); a kg load is logged as the prescribed kg, a
    bodyweight/calibration load as no kg (log the actual weight through "Enter results")."""
    async with db.transaction() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=block)
        if progress is None:
            return Advance(status=status)
        await _ensure_block_rows(conn, session_id, progress.workout, block)
        progress = (await _load_progress(conn, user_id, session_id, block=block))[1]
        assert progress is not None
        for item, rows in zip(
            progress.workout.blocks[block].items, progress.assigned[block], strict=True
        ):
            for row in rows:
                if is_logged(row):
                    continue
                await update_set_log_actual(
                    conn,
                    row.id,
                    actual_load_kg=item.load.kg,
                    actual_reps=item.reps_max,
                    source=_SOURCE_BUTTON,
                )
        return await _advance(conn, progress)


async def skip_block(db: Database, user_id: int, session_id: int, block: int) -> Advance:
    """⏭ Skip: every not-yet-logged set of the block is `skipped = 1` (A§4.2)."""
    async with db.transaction() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=block)
        if progress is None:
            return Advance(status=status)
        await _ensure_block_rows(conn, session_id, progress.workout, block)
        progress = (await _load_progress(conn, user_id, session_id, block=block))[1]
        assert progress is not None
        for rows in progress.assigned[block]:
            for row in rows:
                if not is_logged(row):
                    await mark_set_log_skipped(conn, row.id, source=_SOURCE_BUTTON)
        return await _advance(conn, progress)


async def pain_button(db: Database, user_id: int, session_id: int, block: int) -> HaltResult:
    """⚠ Pain / feeling unwell: always the halt path (A§6.3, A§6.6), stale or not — the
    user just reported a symptom."""
    return await halt(
        db,
        user_id=user_id,
        reason=HealthHoldReason.PAIN_BUTTON,
        guards_fired=[
            GuardVerdict(rule="workout.pain_button", ok=False, detail="pain button pressed")
        ],
        user_report={
            "trigger": "pain_button",
            "event": "halt",
            "button_session_id": session_id,
            "block": block,
        },
    )


async def abort(db: Database, user_id: int, session_id: int | None = None) -> bool:
    """`aborted` for the user's active session (or `session_id` if it is that one); the sets
    stay as logged. `False` when there is nothing to abort."""
    async with db.transaction() as conn:
        active = await get_active_workout_session(conn, user_id)
        if active is None or (session_id is not None and active.id != session_id):
            return False
        await finish_workout_session(conn, active.id, status=WorkoutSessionStatus.ABORTED.value)
    return True


# --- Enter results ----------------------------------------------------------------------------


def _next_item(progress: _Progress, block: int, catalog: Catalog) -> ResultPrompt | None:
    """The first prescription of `block` with an unlogged set, or `None` when the block is
    fully logged."""
    for item_index, (item, rows) in enumerate(
        zip(progress.workout.blocks[block].items, progress.assigned[block], strict=True)
    ):
        if any(not is_logged(row) for row in rows) or not rows:
            return ResultPrompt(
                session_id=progress.ctx.session.id,
                block=block,
                item=item_index,
                prescription=item,
                exercise=catalog.by_id(item.exercise_id),
            )
    return None


async def next_result_prompt(
    db: Database, user_id: int, session_id: int, block: int
) -> ResultPrompt | None:
    """✏️ Enter results: which prescription to ask for (the first with an unlogged set), or
    `None` when the button is stale or the block is already fully logged."""
    async with db.read() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=block)
    if progress is None:
        return None
    return _next_item(progress, block, load_catalog())


async def _log_parse(
    db: Database,
    *,
    user_id: int,
    prompt: ResultPrompt,
    outcome: AgentRunOutcome[ParsedResults],
    llm_input: dict[str, object],
    verdict: str,
    guards_fired: Sequence[GuardVerdict],
) -> int:
    output = outcome.output
    proposal: dict[str, object] = {"verdict": verdict}
    if isinstance(output, Refusal):
        proposal["refusal"] = output.model_dump(mode="json")
        if outcome.record.error_cause is not None:
            proposal["cause"] = outcome.record.error_cause
    else:
        proposal["parsed"] = output.model_dump(mode="json")
    info = outcome.prompt
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.RESULT_PARSE.value,
            prompt_template=None if info is None else info.template_name,
            prompt_version=None if info is None else str(info.version),
            model=outcome.record.model,
            content_version=content_version(),
            llm_input=llm_input,
            user_report={
                "session_id": prompt.session_id,
                "event": _EVENT_PARSE,
                "block": prompt.block,
                "item": prompt.item,
            },
            proposal=proposal,
            guards_fired=[item.model_dump() for item in guards_fired],
        )


async def parse_results(
    db: Database, llm: LlmRuntime, user_id: int, session_id: int, block: int, item: int, text: str
) -> ParseResult:
    """A§6.5 step 5 "Enter results", for one prescription: the stop-word scan **before any
    LLM call** (a hit halts and the model is never invoked) → `result_parse` with only the
    planned block, the text and the load units (A§8.2 per-agent minimization; no history, no
    ids) → `safety_signal` halts (A§7.2: the model can only add a halt) → `unclear` re-asks →
    `guards.plausibility` re-asks → shown for Correct / Fix. Every model call is a
    `result_parse` decision with `llm_input` verbatim."""
    async with db.read() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=block)
        snapshot = await planning.read_snapshot(conn, user_id)
    lang = snapshot.language
    if progress is None or item >= len(progress.workout.blocks[block].items):
        return ParseResult(status=ParseStatus.STALE)
    catalog = load_catalog()
    prescription = progress.workout.blocks[block].items[item]
    exercise = catalog.by_id(prescription.exercise_id)
    prompt = ResultPrompt(
        session_id=session_id, block=block, item=item, prescription=prescription, exercise=exercise
    )

    hit = stop_words.scan(text, lang)
    stop_verdict = stop_words.to_verdict(hit)
    if hit is not None:
        halted = await halt(
            db,
            user_id=user_id,
            reason=HealthHoldReason.STOP_WORD,
            guards_fired=[stop_verdict],
            user_report={"trigger": "stop_word", "event": "halt", "category": hit.category},
        )
        return ParseResult(status=ParseStatus.HALTED, prompt=prompt, halt=halted)

    planned_block = Block(kind="single", items=[prescription])
    load_units = {} if exercise is None else {exercise.id: exercise.load_unit}
    rendered = render_user_prompt(
        language=lang, planned_block=planned_block, result_text=text, load_units=load_units
    )
    spec = model_for("result_parse", llm.settings)
    outcome = await run_agent(
        llm.factory("result_parse"),
        spec.model,
        rendered.text,
        purpose="result_parse",
        model_name=spec.model,
        prices=llm.prices,
        language=lang,
    )
    output = outcome.output
    if isinstance(output, Refusal):
        decision_id = await _log_parse(
            db,
            user_id=user_id,
            prompt=prompt,
            outcome=outcome,
            llm_input=rendered.payload,
            verdict="unavailable",
            guards_fired=[stop_verdict],
        )
        await record_llm_call(db, decision_id=decision_id, record=outcome.record)
        return ParseResult(
            status=ParseStatus.UNAVAILABLE, prompt=prompt, refusal=output, decision_id=decision_id
        )

    layered = combine_with_llm_signal(stop_verdict, output.safety_signal)
    fired: list[GuardVerdict] = [stop_verdict, layered]
    if output.safety_signal:
        decision_id = await _log_parse(
            db,
            user_id=user_id,
            prompt=prompt,
            outcome=outcome,
            llm_input=rendered.payload,
            verdict="safety_signal",
            guards_fired=fired,
        )
        await record_llm_call(db, decision_id=decision_id, record=outcome.record)
        async with db.read() as conn:
            still_active = await get_active_workout_session(conn, user_id)
        if still_active is None or still_active.id != session_id:
            # ⚠ (or a stop word elsewhere) already halted this session while the model was
            # running: one hold is enough, and it is already open.
            return ParseResult(status=ParseStatus.HALTED, prompt=prompt, decision_id=decision_id)
        halted = await halt(
            db,
            user_id=user_id,
            reason=HealthHoldReason.LLM_SAFETY_SIGNAL,
            guards_fired=[layered],
            user_report={
                "trigger": "llm_safety_signal",
                "event": "halt",
                "parse_decision_id": decision_id,
            },
        )
        return ParseResult(
            status=ParseStatus.HALTED, prompt=prompt, halt=halted, decision_id=decision_id
        )

    verdict = _PARSE_SHOWN
    if output.unclear:
        verdict = "unclear"
    elif exercise is not None:
        plausibility = check_parsed_results(
            exercise,
            prescription.load,
            output.sets,
            history_max_kg=snapshot.history_max.get(exercise.id),
        )
        fired.extend(plausibility)
        if any(not item_verdict.ok for item_verdict in plausibility):
            verdict = "implausible"
    if verdict == _PARSE_SHOWN and not _covers_every_set(output.sets, prescription.sets):
        verdict = "unclear"
    decision_id = await _log_parse(
        db,
        user_id=user_id,
        prompt=prompt,
        outcome=outcome,
        llm_input=rendered.payload,
        verdict=verdict,
        guards_fired=fired,
    )
    await record_llm_call(db, decision_id=decision_id, record=outcome.record)
    if verdict == "unclear":
        return ParseResult(status=ParseStatus.UNCLEAR, prompt=prompt, decision_id=decision_id)
    if verdict == "implausible":
        return ParseResult(status=ParseStatus.IMPLAUSIBLE, prompt=prompt, decision_id=decision_id)
    return ParseResult(
        status=ParseStatus.SHOWN, prompt=prompt, parsed=output, decision_id=decision_id
    )


def _covers_every_set(sets: Sequence[SetResult], prescribed_sets: int) -> bool:
    """A usable parse names each prescribed set exactly once (1..sets)."""
    return sorted(item.set_index for item in sets) == list(range(1, prescribed_sets + 1))


async def confirm_results(
    db: Database, user_id: int, session_id: int, block: int, item: int, decision_id: int
) -> ConfirmResults:
    """ "Correct": write the shown parse `decision_id` — which must be the newest parse for
    this prescription, so an older table's Correct is stale — into its rows (only a confirmed
    parse writes `actual_*`), then either ask for the block's next prescription or advance
    the block. A double tap finds the rows already logged and just moves on."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        status, progress = await _load_progress(conn, user_id, session_id, block=block)
        if progress is None or item >= len(progress.workout.blocks[block].items):
            return ConfirmResults(status=status if progress is None else Status.STALE)
        await _ensure_block_rows(conn, session_id, progress.workout, block)
        progress = (await _load_progress(conn, user_id, session_id, block=block))[1]
        assert progress is not None
        prescription = progress.workout.blocks[block].items[item]
        rows = progress.assigned[block][item]
        if not all(is_logged(row) for row in rows):
            decision = await get_latest_session_event_decision(
                conn,
                session_id=session_id,
                kind=DecisionKind.RESULT_PARSE.value,
                event=_EVENT_PARSE,
            )
            parsed = _shown_parse(decision, block=block, item=item)
            if decision is None or parsed is None:
                return ConfirmResults(status=Status.NOT_FOUND)
            if decision.id != decision_id:
                return ConfirmResults(status=Status.STALE)
            by_index = {row.set_index: row for row in rows}
            for result in parsed.sets:
                row = by_index.get(result.set_index)
                if row is None:
                    continue
                if result.skipped:
                    await mark_set_log_skipped(conn, row.id, source=_SOURCE_FREE_TEXT)
                    continue
                assert result.reps is not None  # `SetResult` requires reps unless skipped
                load_kg = result.load_kg if result.load_kg is not None else prescription.load.kg
                await update_set_log_actual(
                    conn,
                    row.id,
                    actual_load_kg=load_kg,
                    actual_reps=result.reps,
                    source=_SOURCE_FREE_TEXT,
                )
            await insert_decision_outcome(
                conn,
                decision_id=decision.id,
                outcome={"confirmed": True, "set_log_ids": [row.id for row in rows]},
            )
        progress = (await _load_progress(conn, user_id, session_id, block=block))[1]
        assert progress is not None
        next_prompt = _next_item(progress, block, catalog)
        if next_prompt is not None:
            return ConfirmResults(status=Status.OK, next_prompt=next_prompt)
        return ConfirmResults(status=Status.OK, advance=await _advance(conn, progress))


def _shown_parse(decision: DecisionRecord | None, *, block: int, item: int) -> ParsedResults | None:
    if decision is None or decision.proposal is None or decision.user_report is None:
        return None
    if decision.user_report.get("block") != block or decision.user_report.get("item") != item:
        return None
    if decision.proposal.get("verdict") != _PARSE_SHOWN:
        return None
    body = decision.proposal.get("parsed")
    if body is None:
        return None
    try:
        return ParsedResults.model_validate(body)
    except ValueError:
        return None


__all__ = [
    "ActiveSession",
    "Advance",
    "AdjustResult",
    "BlockView",
    "ChoosePlan",
    "Completion",
    "ConfirmResults",
    "Entry",
    "NoPlan",
    "ParseResult",
    "ParseStatus",
    "PrecheckResult",
    "Refused",
    "ResultPrompt",
    "ReviewView",
    "SaveResult",
    "SessionCreated",
    "StartResult",
    "Status",
    "WorkoutSuggested",
    "abort",
    "active_session",
    "adjust",
    "complete_block_as_planned",
    "confirm_results",
    "create_session",
    "current_block",
    "entry",
    "next_result_prompt",
    "pain_button",
    "parse_results",
    "precheck_no",
    "precheck_yes",
    "review",
    "save_to_plan",
    "skip_block",
    "start",
    "suggest_workout",
]
