"""The end-of-workout recap (A§6.5 step 6, IMPLEMENTATION_PLAN M8), UI-agnostic. Runs once
per completed session, right after `services.training` marks it `completed`.

1. **Deterministic summary** per exercise: completed vs planned sets, skipped sets, reps,
   volume (Σ reps × kg over the sets logged with a kg load), and whether the session set a
   new historical max. Neutral numbers only: no streaks, no shame (AGENTS.md §4).
2. **Check-ins**: `services.training._complete` creates one `checkins` row per **flagged**
   area that today's exercises load, `unknown`, in the same transaction that completes the
   session; they are answered only by an explicit button here (`answer_checkin`). An
   unanswered check-in stays `unknown`, and `guards.checkins.increase_allowed` (used by the
   load engine) blocks the next increase for that area: silence is not consent. **Pain**
   always runs the halt path with reason `checkin_pain`, even after an earlier answer.
3. **Progression preview**: the load engine (`planning.engine_decision`, over a fresh
   snapshot read after the check-ins were created, so today's flagged areas count as
   `unknown`) computes every exercise's next load with its structured kind, reason and
   guard verdicts; a hold that only waits on a check-in says what the load will be "if the
   check-in is fine". **It is a preview, not an application.** Loads are applied at the
   next session's Start (`services.training.start` writes the applying
   `session_adjust`/`start` decision with the `load_changes`, A§4.3 "counted once, when
   applied"). The recap therefore writes `decision(kind=progression)` with `proposal` = the
   preview and `load_changes = []`; the weekly cap never sees a preview.
4. **Recap text**: the `recap` agent (`run_agent`, no escalation; input through
   `render_user_prompt(language, recap=...)`: planned vs actual per exercise and the engine's
   decisions, no ids or history beyond that) writes a short explanation and may suggest
   structural `PlanChange`s (swap an exercise, change a rep range). The numbers shown are
   the engine's from the preview, never the model's; the model text is dropped (and the
   drop logged) if it fails the wording check, the display length cap, or names a kg figure
   that is in neither the summary nor the preview. Suggestions are filtered at recap time
   with `guards.plan.prescription_verdicts` (a swap target must be allowed); dropped ones
   are logged. A provider failure (`LLM_UNAVAILABLE`) shows the deterministic recap alone.
5. **Apply** (`apply_suggestion`): the suggestion is applied to the plan's latest version,
   the whole plan is judged with `planning.judge` (the same load-substitution path as
   `/plan`: stale stored loads get the engine's current value, structural failures refuse)
   and, if it passes, a `plan_confirm` decision with the judged plan's `load_changes`
   (computed; empty for a non-load change) and a `plan_versions` row with
   `origin=progression` are written. Stale-safe (only the user's newest recap decision
   applies) and idempotent (the recap decision's `decision_outcomes` record what was
   applied).
6. **`decision_outcomes`**: `_complete` writes one on the session's `start` decision with
   planned vs actual per set (AGENTS.md §6 "what the user actually did"); the progression
   decision's `user_report` records the check-in answers as they are when it is written.
   `pending_recap_session` finds a completed session whose recap was never shown (no
   progression decision, e.g. a crash mid-recap) so `/train` and `/start` show it.

Unit-of-work rules (A§4.6): read → close → LLM → transaction → write. The recap is
idempotent: a second call (e.g. after a restart) finds the session's progression decision and
rebuilds the view from it without another model call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome
from fitme.db.controllers.plans import insert_plan_version
from fitme.db.controllers.training import answer_checkin as write_checkin_answer
from fitme.db.records import CheckinRecord, DecisionRecord, SetLogRecord
from fitme.db.selectors.decisions import (
    get_decision,
    get_latest_decision_of_kind,
    get_latest_session_event_decision,
    list_decision_outcomes,
)
from fitme.db.selectors.plans import get_latest_plan_version
from fitme.db.selectors.training import (
    get_checkin,
    historical_max_by_exercise_before_session,
    list_checkins_for_session,
    list_set_logs_for_session,
    list_workout_sessions_for_user,
)
from fitme.domain.catalog import Catalog
from fitme.domain.enums import (
    CheckinAnswer,
    DecisionKind,
    HealthHoldReason,
    RefusalCode,
    WorkoutSessionStatus,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load, Plan, Prescription, Refusal, Workout
from fitme.domain.results import ChangeReps, PlanChange, SwapExercise
from fitme.guards.plan import prescription_verdicts
from fitme.i18n import wording
from fitme.llm.context import render_user_prompt
from fitme.llm.models import model_for
from fitme.llm.usage import record_llm_call, run_agent
from fitme.services import planning
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.loads import BlockedBy, LoadDecision, LoadKind
from fitme.services.safety import HaltResult, halt
from fitme.services.training import (
    EVENT_START,
    Status,
    assign_rows,
    load_session,
    started_workout,
)

_EVENT_RECAP = "recap"
_ORIGIN_PROGRESSION = "progression"
_AREA_QUESTION_PREFIX = "area:"
_WORDING_RULE = "wording.forbidden_term"
_LENGTH_RULE = "recap.text_length"
_FIGURE_RULE = "recap.unknown_figure"
_SUGGESTION_RULE = "recap.suggestion_dropped"
# A kg figure in the model's text: "42.5 kg", "42,5kg", "40 кг".
_KG_FIGURE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kg|кг)\b", re.IGNORECASE)
_RECAP_TEXT_MAX_CHARS = 700  # display cap: a few sentences, never a wall of text

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False)


# --- Proposal models (stored verbatim in `decisions.proposal`) --------------------------------


class NextKind(StrEnum):
    INCREASE = "increase"
    HOLD = "hold"
    HOLD_BLOCKED = "hold_blocked"  # a guard (cap, ceiling) held an earned increase
    HOLD_CHECKIN = "hold_checkin"  # only a check-in holds it: `if_fine` says what it would be
    DECREASE = "decrease"
    CLAMPED = "clamped"  # adjusted down to the ceiling (what the user has logged so far)
    CALIBRATION = "calibration"
    BODYWEIGHT = "bodyweight"


class ExerciseSummary(BaseModel):
    model_config = _STRICT

    exercise_id: str
    planned_sets: int
    done_sets: int
    skipped_sets: int
    planned_reps_min: int
    planned_reps_max: int
    planned_load: Load
    actual_reps: list[int | None]
    actual_kg: list[float | None]
    total_reps: int
    volume_kg: float
    new_max_kg: float | None = None


class NextLoad(BaseModel):
    model_config = _STRICT

    exercise_id: str
    current: Load  # this session's prescription
    next: Load  # the engine's value for the next session
    kind: NextKind
    reason: str  # the engine's own reason, machine text
    if_fine: Load | None = None  # `HOLD_CHECKIN`: the load once the check-in is answered fine


class RecapProposal(BaseModel):
    """`decisions.proposal` of the `progression` decision: the preview and the summary the
    bot shows, plus the model's text/suggestions (or its refusal)."""

    model_config = _STRICT

    summary: list[ExerciseSummary]
    preview: list[NextLoad]
    recap_text: str | None = None
    suggestions: list[PlanChange] = Field(default_factory=list)
    refusal: Refusal | None = None


# --- Result types -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecapView:
    session_id: int
    decision_id: int  # the `progression` decision (Apply buttons reference it)
    workout: Workout
    plan_name: str
    proposal: RecapProposal
    checkins: list[CheckinRecord]


@dataclass(frozen=True, slots=True)
class CheckinResult:
    status: Status
    halt: HaltResult | None = None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: Status
    plan_name: str | None = None
    version: int | None = None
    refusal: Refusal | None = None


# --- Summary and preview (pure) ---------------------------------------------------------------


def _summaries(
    workout: Workout,
    rows: Sequence[SetLogRecord],
    previous_max: dict[str, float],
    catalog: Catalog,
) -> list[ExerciseSummary]:
    """Per prescription: sets done/skipped, reps, volume and a new max. Volume is Σ reps × kg
    over the sets logged with a kg load; on a `per_implement` exercise the logged kg is one
    dumbbell/kettlebell, so both implements count (× 2)."""
    assigned = assign_rows(rows, workout)
    summaries: list[ExerciseSummary] = []
    session_max: dict[str, float] = {}
    for block, per_item in zip(workout.blocks, assigned, strict=True):
        for item, item_rows in zip(block.items, per_item, strict=True):
            done = [row for row in item_rows if row.actual_reps is not None]
            exercise = catalog.by_id(item.exercise_id)
            implements = 2 if exercise is not None and exercise.load_unit == "per_implement" else 1
            volume = sum(
                row.actual_reps * row.actual_load_kg * implements
                for row in done
                if row.actual_load_kg is not None and row.actual_reps is not None
            )
            for row in done:
                if row.actual_load_kg is not None:
                    session_max[item.exercise_id] = max(
                        session_max.get(item.exercise_id, 0.0), row.actual_load_kg
                    )
            summaries.append(
                ExerciseSummary(
                    exercise_id=item.exercise_id,
                    planned_sets=item.sets,
                    done_sets=len(done),
                    skipped_sets=sum(1 for row in item_rows if row.skipped),
                    planned_reps_min=item.reps_min,
                    planned_reps_max=item.reps_max,
                    planned_load=item.load,
                    actual_reps=[row.actual_reps for row in item_rows],
                    actual_kg=[row.actual_load_kg for row in item_rows],
                    total_reps=sum(row.actual_reps for row in done if row.actual_reps is not None),
                    volume_kg=round(volume, 3),
                )
            )
    for summary in summaries:
        best = session_max.get(summary.exercise_id)
        before = previous_max.get(summary.exercise_id)
        if best is not None and (before is None or best > before):
            summary.new_max_kg = best
    return summaries


_KINDS: dict[LoadKind, NextKind] = {
    LoadKind.INCREASE: NextKind.INCREASE,
    LoadKind.HOLD: NextKind.HOLD,
    LoadKind.HOLD_BLOCKED: NextKind.HOLD_BLOCKED,
    LoadKind.DECREASE: NextKind.DECREASE,
    LoadKind.CLAMPED: NextKind.CLAMPED,
    LoadKind.CALIBRATION: NextKind.CALIBRATION,
    LoadKind.BODYWEIGHT: NextKind.BODYWEIGHT,
}


def _next_kind(decision: LoadDecision) -> tuple[NextKind, Load | None]:
    """The recap kind from the engine's structured decision. A hold whose *only* failing
    guard is the check-in gate becomes `HOLD_CHECKIN` with the load it would be if the
    check-in is fine (the increase every other guard already accepted)."""
    kind = _KINDS[decision.kind]
    if (
        kind == NextKind.HOLD_BLOCKED
        and decision.blocked_by == BlockedBy.CHECKIN
        and decision.load.kg is not None
        and all(
            verdict.ok
            for verdict in decision.guards_fired
            if verdict.rule != "checkins.increase_allowed"
        )
    ):
        return NextKind.HOLD_CHECKIN, None  # `if_fine` is filled by the caller (needs the step)
    return kind, None


def _preview(
    workout: Workout, inputs: planning.Inputs
) -> tuple[list[NextLoad], list[GuardVerdict]]:
    """The engine's next load per distinct exercise, in workout order (A§7.3)."""
    preview: list[NextLoad] = []
    fired: list[GuardVerdict] = []
    seen: set[str] = set()
    for block in workout.blocks:
        for item in block.items:
            if item.exercise_id in seen:
                continue
            seen.add(item.exercise_id)
            exercise = inputs.catalog.by_id(item.exercise_id)
            if exercise is None:
                continue
            decision = planning.engine_decision(exercise, inputs)
            fired.extend(decision.guards_fired)
            kind, _unused = _next_kind(decision)
            if_fine = None
            if kind == NextKind.HOLD_CHECKIN and decision.load.kg is not None:
                if_fine = Load(kind="kg", kg=decision.load.kg + exercise.increment_kg)
            preview.append(
                NextLoad(
                    exercise_id=exercise.id,
                    current=item.load,
                    next=decision.load,
                    kind=kind,
                    reason=decision.reason,
                    if_fine=if_fine,
                )
            )
    return preview, fired


def _recap_input(
    summaries: Sequence[ExerciseSummary], preview: Sequence[NextLoad], catalog: Catalog
) -> dict[str, object]:
    """The `recap` agent's input (A§8.2 minimization): catalog ids and this session's numbers,
    the engine's decisions with their reasons, and each exercise's `load_unit`."""
    return {
        "exercises": [
            {
                "exercise_id": item.exercise_id,
                "load_unit": getattr(catalog.by_id(item.exercise_id), "load_unit", "total"),
                "planned_sets": item.planned_sets,
                "planned_reps": [item.planned_reps_min, item.planned_reps_max],
                "planned_load": item.planned_load.model_dump(mode="json"),
                "done_sets": item.done_sets,
                "skipped_sets": item.skipped_sets,
                "actual_reps": item.actual_reps,
                "actual_kg": item.actual_kg,
                "new_max_kg": item.new_max_kg,
            }
            for item in summaries
        ],
        "next_session": [
            {
                "exercise_id": item.exercise_id,
                "current": item.current.model_dump(mode="json"),
                "next": item.next.model_dump(mode="json"),
                "kind": item.kind.value,
                "reason": item.reason,
            }
            for item in preview
        ],
    }


def _known_kg(summaries: Sequence[ExerciseSummary], preview: Sequence[NextLoad]) -> set[float]:
    """Every kg figure the deterministic recap itself states (loads, logged kg, new max,
    volume): the only figures the model's text may mention."""
    known: set[float] = set()
    for item in summaries:
        known.update(kg for kg in item.actual_kg if kg is not None)
        if item.planned_load.kg is not None:
            known.add(item.planned_load.kg)
        if item.new_max_kg is not None:
            known.add(item.new_max_kg)
        known.add(item.volume_kg)
    for step in preview:
        for load in (step.current, step.next, step.if_fine):
            if load is not None and load.kg is not None:
                known.add(load.kg)
    return known


def _check_text(text: str, known_kg: set[float]) -> tuple[str | None, list[GuardVerdict]]:
    """AGENTS.md §3 wording, the display length cap, and a figure check (a kg number in
    neither the summary nor the preview) over the model's text: a failing text is dropped
    (the deterministic recap stands alone) and the drop is logged."""
    for match in _KG_FIGURE_RE.finditer(text):
        figure = float(match.group(1).replace(",", "."))
        if not any(abs(figure - kg) < 1e-6 for kg in known_kg):
            return None, [
                GuardVerdict(
                    rule=_FIGURE_RULE,
                    ok=False,
                    detail=f"recap text: {figure:g} kg is not a recap figure; dropped",
                )
            ]
    term = wording.first_forbidden_term(text)
    if term is not None:
        return None, [
            GuardVerdict(
                rule=_WORDING_RULE, ok=False, detail=f"recap text: {term!r} found; dropped"
            )
        ]
    if len(text) > _RECAP_TEXT_MAX_CHARS:
        return None, [
            GuardVerdict(
                rule=_LENGTH_RULE,
                ok=False,
                detail=f"recap text: {len(text)} chars > {_RECAP_TEXT_MAX_CHARS}; dropped",
            )
        ]
    return text, []


def _filter_suggestions(
    suggestions: Sequence[PlanChange], plan: Plan, inputs: planning.Inputs
) -> tuple[list[PlanChange], list[GuardVerdict]]:
    """Keep only suggestions that can pass the guards: a swap's target must be an allowed
    prescription (`guards.plan.prescription_verdicts` at the engine's load), a rep change
    must name an exercise in the plan and stay valid. Dropped ones are logged."""
    kept: list[PlanChange] = []
    fired: list[GuardVerdict] = []
    prescriptions = [
        item for workout in plan.workouts for block in workout.blocks for item in block.items
    ]
    for change in suggestions:
        if isinstance(change, SwapExercise):
            source = next(
                (p for p in prescriptions if p.exercise_id == change.from_exercise_id), None
            )
            target = inputs.catalog.by_id(change.to_exercise_id)
            if source is None or target is None:
                fired.append(
                    GuardVerdict(
                        rule=_SUGGESTION_RULE,
                        ok=False,
                        detail=(
                            f"swap {change.from_exercise_id} -> {change.to_exercise_id} dropped: "
                            "not in the plan or not a catalog exercise"
                        ),
                    )
                )
                continue
            candidate = Prescription(
                exercise_id=target.id,
                sets=source.sets,
                reps_min=source.reps_min,
                reps_max=source.reps_max,
                load=planning.engine_load(target, inputs)[0],
                rest_seconds=source.rest_seconds,
            )
        else:
            source = next((p for p in prescriptions if p.exercise_id == change.exercise_id), None)
            if source is None:
                fired.append(
                    GuardVerdict(
                        rule=_SUGGESTION_RULE,
                        ok=False,
                        detail=f"rep change for {change.exercise_id} dropped: not in the plan",
                    )
                )
                continue
            candidate = source.model_copy(
                update={"reps_min": change.reps_min, "reps_max": change.reps_max}
            )
        failures = [v for v in prescription_verdicts(candidate, inputs.ctx) if not v.ok]
        if failures:
            fired.append(
                GuardVerdict(
                    rule=_SUGGESTION_RULE,
                    ok=False,
                    detail=f"{change.kind} dropped: " + "; ".join(v.detail for v in failures),
                )
            )
            continue
        kept.append(change)
    return kept, fired


def _proposal_of(decision: DecisionRecord) -> RecapProposal | None:
    if decision.proposal is None:
        return None
    try:
        return RecapProposal.model_validate(decision.proposal)
    except ValueError:
        return None


# --- Build the recap ------------------------------------------------------------------------------


async def build_recap(
    db: Database, llm: LlmRuntime, user_id: int, session_id: int
) -> RecapView | None:
    """See the module docstring. `None` for a session that isn't this user's or isn't
    `completed`."""
    catalog = load_catalog()
    async with db.read() as conn:
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None or ctx.session.status != WorkoutSessionStatus.COMPLETED.value:
            return None
        workout = await started_workout(conn, session_id)
        if workout is None:
            workout = ctx.workout
        existing = await get_latest_session_event_decision(
            conn, session_id=session_id, kind=DecisionKind.PROGRESSION.value, event=_EVENT_RECAP
        )
        checkins = await list_checkins_for_session(conn, session_id)
        if existing is not None:
            stored = _proposal_of(existing)
            if stored is not None:
                return RecapView(
                    session_id=session_id,
                    decision_id=existing.id,
                    workout=workout,
                    plan_name=ctx.plan_record.name,
                    proposal=stored,
                    checkins=checkins,
                )
        rows = await list_set_logs_for_session(conn, session_id)
        previous_max = await historical_max_by_exercise_before_session(conn, user_id, session_id)
        snapshot = await planning.read_snapshot(conn, user_id)
    lang = snapshot.language
    if snapshot.profile is None:
        return None
    inputs = planning.build_inputs(catalog, snapshot, llm.settings, user_id)
    summaries = _summaries(workout, rows, previous_max, catalog)
    preview, fired = _preview(workout, inputs)

    rendered = render_user_prompt(language=lang, recap=_recap_input(summaries, preview, catalog))
    spec = model_for("recap", llm.settings)
    outcome = await run_agent(
        llm.factory("recap"),
        spec.model,
        rendered.text,
        purpose="recap",
        model_name=spec.model,
        prices=llm.prices,
        language=lang,
    )
    output = outcome.output
    if isinstance(output, Refusal):
        proposal = RecapProposal(summary=summaries, preview=preview, refusal=output)
    else:
        text, text_verdicts = _check_text(output.text, _known_kg(summaries, preview))
        suggestions, suggestion_verdicts = _filter_suggestions(output.suggestions, ctx.plan, inputs)
        fired.extend([*text_verdicts, *suggestion_verdicts])
        proposal = RecapProposal(
            summary=summaries, preview=preview, recap_text=text, suggestions=suggestions
        )

    async with db.transaction() as conn:
        prompt = outcome.prompt
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.PROGRESSION.value,
            prompt_template=None if prompt is None else prompt.template_name,
            prompt_version=None if prompt is None else str(prompt.version),
            model=outcome.record.model,
            content_version=content_version(),
            llm_input=rendered.payload,
            user_report={
                "session_id": session_id,
                "event": _EVENT_RECAP,
                "checkins": {
                    checkin.question_key.removeprefix(_AREA_QUESTION_PREFIX): checkin.answer
                    for checkin in checkins
                },
            },
            proposal=proposal.model_dump(mode="json"),
            guards_fired=[verdict.model_dump() for verdict in fired],
            load_changes=[],  # a preview: applied at the next Start (A§4.3)
        )
    await record_llm_call(db, decision_id=decision_id, record=outcome.record)
    return RecapView(
        session_id=session_id,
        decision_id=decision_id,
        workout=workout,
        plan_name=ctx.plan_record.name,
        proposal=proposal,
        checkins=checkins,
    )


async def pending_recap_session(db: Database, user_id: int) -> int | None:
    """The user's newest session if it is `completed`, was run through the workout loop (it
    has a `start` decision; imported or seeded history has none and gets no recap) and its
    recap was never written (no `progression` decision): `/train` and `/start` show that
    recap first."""
    async with db.read() as conn:
        sessions = await list_workout_sessions_for_user(conn, user_id)
        if not sessions or sessions[0].status != WorkoutSessionStatus.COMPLETED.value:
            return None
        newest = sessions[0]
        started = await get_latest_session_event_decision(
            conn, session_id=newest.id, kind=DecisionKind.SESSION_ADJUST.value, event=EVENT_START
        )
        existing = await get_latest_session_event_decision(
            conn, session_id=newest.id, kind=DecisionKind.PROGRESSION.value, event=_EVENT_RECAP
        )
    return None if started is None or existing is not None else newest.id


# --- Check-ins ------------------------------------------------------------------------------------


async def answer_checkin(
    db: Database, user_id: int, checkin_id: int, answer: CheckinAnswer
) -> CheckinResult:
    """One explicit button answer (A§6.5 step 6). `ALREADY` for a check-in already answered
    (a double tap changes nothing), `STALE` once a newer session exists, `NOT_FOUND` for a
    check-in that isn't this user's. **Pain always runs the halt path** (`checkin_pain`),
    like ⚠ and the precheck Yes — after an earlier answer or a newer session too; the stored
    answer is written only while it is still `unknown`."""
    if answer == CheckinAnswer.UNKNOWN:
        return CheckinResult(status=Status.NOT_FOUND)
    async with db.transaction() as conn:
        checkin = await get_checkin(conn, checkin_id)
        if checkin is None or checkin.user_id != user_id:
            return CheckinResult(status=Status.NOT_FOUND)
        unanswered = checkin.answer == CheckinAnswer.UNKNOWN.value
        sessions = await list_workout_sessions_for_user(conn, user_id)
        stale = bool(
            sessions and checkin.session_id is not None and sessions[0].id != checkin.session_id
        )
        if answer != CheckinAnswer.PAIN:
            if not unanswered:
                return CheckinResult(status=Status.ALREADY)
            if stale:
                return CheckinResult(status=Status.STALE)
        if unanswered and not stale:
            await write_checkin_answer(conn, checkin_id, answer=answer.value)
    if answer != CheckinAnswer.PAIN:
        return CheckinResult(status=Status.OK)
    area = checkin.question_key.removeprefix(_AREA_QUESTION_PREFIX)
    halted = await halt(
        db,
        user_id=user_id,
        reason=HealthHoldReason.CHECKIN_PAIN,
        guards_fired=[
            GuardVerdict(rule="checkin.answer", ok=False, detail=f"{area}: pain reported")
        ],
        user_report={
            "trigger": "checkin_pain",
            "event": "halt",
            "checkin_id": checkin_id,
            "area": area,
            "checkin_session_id": checkin.session_id,
        },
        source_session_id=checkin.session_id,
    )
    return CheckinResult(status=Status.OK, halt=halted)


# --- Apply a suggestion --------------------------------------------------------------------------


def _apply_change(plan: Plan, change: PlanChange, inputs: planning.Inputs) -> bool:
    """Apply `change` to `plan` in place (every prescription of the exercise, in every
    workout). A swapped-in exercise gets the load engine's value (A§7.3: calibration with
    no history). Returns whether anything changed."""
    changed = False
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                if isinstance(change, SwapExercise):
                    if prescription.exercise_id != change.from_exercise_id:
                        continue
                    exercise = inputs.catalog.by_id(change.to_exercise_id)
                    prescription.exercise_id = change.to_exercise_id
                    if exercise is not None:
                        prescription.load = planning.engine_load(exercise, inputs)[0]
                    changed = True
                elif isinstance(change, ChangeReps):
                    if prescription.exercise_id != change.exercise_id:
                        continue
                    prescription.reps_min = change.reps_min
                    prescription.reps_max = change.reps_max
                    changed = True
    return changed


async def apply_suggestion(
    db: Database, settings: Settings, user_id: int, session_id: int, decision_id: int, index: int
) -> ApplyResult:
    """Apply suggestion `index` of the session's recap decision `decision_id` to the plan
    (see the module docstring, step 5). One transaction; no network inside."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        decision = await get_decision(conn, decision_id)
        if (
            decision is None
            or decision.user_id != user_id
            or decision.kind != DecisionKind.PROGRESSION.value
            or decision.user_report is None
            or decision.user_report.get("session_id") != session_id
        ):
            return ApplyResult(status=Status.NOT_FOUND)
        latest = await get_latest_decision_of_kind(conn, user_id, DecisionKind.PROGRESSION.value)
        if latest is None or latest.id != decision_id:
            return ApplyResult(status=Status.STALE)  # a later session's recap supersedes it
        proposal = _proposal_of(decision)
        if proposal is None or not 0 <= index < len(proposal.suggestions):
            return ApplyResult(status=Status.NOT_FOUND)
        ctx = await load_session(conn, user_id, session_id)
        if ctx is None:
            return ApplyResult(status=Status.NOT_FOUND)
        for outcome in await list_decision_outcomes(conn, decision_id):
            if outcome.outcome.get("applied_index") == index:
                version_number = outcome.outcome.get("version")
                return ApplyResult(
                    status=Status.ALREADY,
                    plan_name=ctx.plan_record.name,
                    version=version_number if isinstance(version_number, int) else None,
                )
        snapshot = await planning.read_snapshot(conn, user_id)
        lang = snapshot.language
        failure = planning.gate(snapshot)
        if failure is not None:
            code, verdict = failure
            refusal = planning.refusal_for(code, lang)
            await planning.write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=[verdict],
                user_report={"flow": "recap", "session_id": session_id, "apply_of": decision_id},
            )
            return ApplyResult(status=Status.REFUSED, refusal=refusal)
        inputs = planning.build_inputs(catalog, snapshot, settings, user_id)
        latest_version = await get_latest_plan_version(conn, ctx.plan_record.id)
        base = ctx.plan if latest_version is None else Plan.model_validate(latest_version.body)
        modified = base.model_copy(deep=True)
        if not _apply_change(modified, proposal.suggestions[index], inputs):
            return ApplyResult(status=Status.NOT_FOUND)  # the exercise isn't in the plan any more
        # The same judging as a `/plan` confirm: a stored load that no longer passes (e.g.
        # after an engine decrease) gets the engine's current value; structural failures refuse.
        judgement = planning.judge(modified, inputs)
        if not judgement.ok:
            refusal = planning.refusal_for(RefusalCode.NO_SAFE_PLAN, lang)
            await planning.write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=judgement.failures,
                user_report={
                    "flow": "recap",
                    "session_id": session_id,
                    "apply_of": decision_id,
                    "applied_index": index,
                },
                rejected_plan=modified,
            )
            return ApplyResult(status=Status.REFUSED, refusal=refusal)
        version = 1 if latest_version is None else latest_version.version + 1
        load_changes = planning.load_changes_for(modified, inputs.ctx)
        confirm_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.PLAN_CONFIRM.value,
            prompt_template=decision.prompt_template,
            prompt_version=decision.prompt_version,
            model=decision.model,
            content_version=content_version(),
            llm_input=None,
            user_report={
                "flow": "recap",
                "session_id": session_id,
                "progression_decision_id": decision_id,
                "applied_index": index,
                "plan_id": ctx.plan_record.id,
            },
            proposal=planning.draft_proposal(modified, load_changes),
            guards_fired=[verdict.model_dump() for verdict in judgement.fired],
            load_changes=load_changes,
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=ctx.plan_record.id,
            version=version,
            body=modified.model_dump(mode="json"),
            origin=_ORIGIN_PROGRESSION,
            decision_id=confirm_id,
        )
        outcome_body: dict[str, object] = {
            "applied_index": index,
            "plan_id": ctx.plan_record.id,
            "plan_version_id": version_id,
            "version": version,
            "confirm_decision_id": confirm_id,
        }
        await insert_decision_outcome(conn, decision_id=decision_id, outcome=outcome_body)
        await insert_decision_outcome(conn, decision_id=confirm_id, outcome=outcome_body)
    return ApplyResult(status=Status.OK, plan_name=ctx.plan_record.name, version=version)


def recap_of_decision(decision: DecisionRecord) -> RecapProposal | None:
    """The stored recap proposal of a `progression` decision (for tests and the web later)."""
    return _proposal_of(decision)


__all__ = [
    "ApplyResult",
    "CheckinResult",
    "ExerciseSummary",
    "NextKind",
    "NextLoad",
    "RecapProposal",
    "RecapView",
    "answer_checkin",
    "apply_suggestion",
    "build_recap",
    "pending_recap_session",
    "recap_of_decision",
]
