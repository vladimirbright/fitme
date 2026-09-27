"""The `/plan` use case (A§6.4), UI-agnostic: the Telegram bot now (M6), the website in M9.

The flow, per A§6.4 and A§4.6's unit-of-work rules (read → close → LLM → transaction → write;
never nest; never await network inside a unit):

1. **Gates** (no LLM call on failure): the profile is complete and
   `guards.screening.plan_allowed` passes (screening complete, clearance where needed, no open
   health hold). A failure is a `Refusal`, logged as `decision(kind=refusal)`.
2. **Context**, read in ONE `db.read()`: flags, holds, location/equipment, frequency, the
   per-exercise history (historical max, the last two completed sessions' outcomes), the
   latest check-in answers, the trailing-7-day increases from `decisions.load_changes`, and
   the default plan's latest version. From it: the `GuardContext` (A§7) and the pseudonymized
   `UserContext` (A§8.2, via `llm.context` — the only rendering path).
3. **LLM**, outside any unit of work: `plan_generate` through `run_agent` (one guard-feedback
   retry, A§6.4 step 4), or `plan_revise` through `run_with_escalation` (A§8.5 rule 3: a
   guard-feedback retry, then one large-tier attempt, then refusal).
4. **Judge** (`judge`, pure): `guards.plan.validate_plan`. A§7.3: when the *only* failing
   verdicts are load rules (`guards.plan.LOAD_RULES`), the offending prescriptions get the
   load engine's value instead (`services.loads.next_load`; `calibration` for an exercise
   with no history — the engine never emits kg without history) and no retry is spent; the
   rejection is recorded in `guards_fired`.
   Structural failures (non-catalog id, contraindication, equipment/location, schedule) use
   the retry. After any substitution the plan is re-validated and must pass fully.
5. **Log**: every LLM attempt is its own immutable `decisions` row (AGENTS.md §6) with the
   template/version and resolved model from the run outcome, `content_version`, the exact
   rendered payload as `llm_input`, the proposal and `guards_fired`. **Load changes count
   once, when applied (A§4.3):** a draft's `proposal` shows its `proposed_load_changes` (every
   prescribed kg above the reference load, A§7: `min(current, history_max)`) for display, but
   its `load_changes` column is `[]`; only the `plan_confirm` decision written inside the
   confirm transaction carries `load_changes`, computed against the confirm-time reference,
   so the weekly cap counts a change exactly once and never counts a draft that was never
   confirmed. A round that ends in a guard refusal adds one `decision(kind=refusal)`
   carrying the final failures.
6. **Wording**: model-authored display text (plan name, workout titles, notes) is checked
   against the AGENTS.md §3 term lists (`fitme.i18n.wording`) at intake: a failing note is
   dropped, a failing name/title is replaced with a neutral default, and the replacement is
   recorded in `guards_fired`.

**Shared with `/train` (M7).** `read_snapshot`, `gate`, `build_inputs`, `judge`,
`engine_load`, `load_changes_for` and the decision-shaping helpers are public so
`services.training` runs the *same* gates, guard context, load engine and load-change
accounting over a workout as `/plan` runs over a plan (A§2.1: one code path, one set of
guards).

**Drafts.** An unconfirmed draft is never a `plans` row: it *is* the `Plan` stored in its
decision's `proposal`. Confirm references the decision id, re-validates the plan against a
fresh `GuardContext` (state may have changed: a new hold, new history), and only then writes
`decision(kind=plan_confirm)` + `plans` + `plan_versions` (linked to the confirm decision;
the draft's id is in its `user_report`) — idempotently, and only for the latest round (a
stale Confirm is rejected). Revising an existing plan produces a draft that, on confirm,
becomes `version n+1` of that plan.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from fitme import clock, i18n
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Connection, Database
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome
from fitme.db.controllers.plans import (
    clear_default_plan,
    insert_plan,
    insert_plan_version,
    set_default_plan,
    update_plan_status,
)
from fitme.db.records import DecisionRecord, PlanRecord, PlanVersionRecord, SessionOutcome
from fitme.db.selectors.decisions import (
    applied_to_kg_by_exercise,
    get_decision,
    get_latest_plan_round_decision,
    list_decision_outcomes,
    recent_increase_deltas_by_exercise,
)
from fitme.db.selectors.plans import (
    get_default_plan,
    get_latest_plan_version,
    get_plan,
    get_plan_version,
    list_plans_for_user,
)
from fitme.db.selectors.profile import get_profile, list_screening_flags
from fitme.db.selectors.training import (
    historical_max_by_exercise,
    latest_checkin_answers,
    list_open_health_holds,
    recent_session_outcomes_by_exercise,
)
from fitme.db.selectors.users import get_user
from fitme.domain.catalog import LOCATION_DEFAULT_EQUIPMENT, Catalog, Exercise
from fitme.domain.enums import (
    AgeBucket,
    BarbellExperience,
    CheckinAnswer,
    DecisionKind,
    Equipment,
    Experience,
    Focus,
    HealthHoldReason,
    Location,
    Preference,
    RefusalCode,
    ScreeningFlag,
    WeightBucket,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load, LoadChange, Plan, Refusal, Workout
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import (
    LOAD_RULES,
    load_verdicts,
    reference_load_kg,
    validate_plan,
    validate_workout,
)
from fitme.guards.screening import plan_allowed
from fitme.i18n import wording
from fitme.llm.context import (
    ExerciseHistorySummary,
    FlagSummary,
    UserContext,
    build_user_context,
    render_user_prompt,
)
from fitme.llm.escalation import run_with_escalation
from fitme.llm.models import model_for
from fitme.llm.usage import AgentRunOutcome, record_llm_call, run_agent
from fitme.services.catalog import available_exercises
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.loads import ExerciseHistory, LoadDecision, next_load

_logger = logging.getLogger(__name__)

_GENERATE_ATTEMPTS = 2  # A§6.4 step 4: one attempt plus one guard-feedback retry
_DRAFT_KINDS = frozenset({DecisionKind.PLAN_GENERATE, DecisionKind.PLAN_REVISE})
_SCREENING_INCOMPLETE_RULE = "screening.incomplete"  # guards.screening's own rule name
_PROFILE_RULE = "plan.profile_complete"
_ALLOWED_EXERCISES_RULE = "plan.allowed_exercises"
_SUBSTITUTION_RULE = "loads.substituted"
_WORDING_RULE = "wording.forbidden_term"
_PLAN_STATUS_ACTIVE = "active"
_PLAN_STATUS_ARCHIVED = "archived"
_ORIGIN_LLM = "llm"
_MAX_SUMMARY_REPS = 100  # `llm.context.ExerciseHistorySummary.last_reps` upper bound


# --- Public result types ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanRoundResult:
    """One `/plan` round's outcome: a draft `Plan` (confirm it by `decision_id`) or a
    `Refusal` (AGENTS.md §2: a valid output). `plan_id` is set when the draft revises an
    existing plan (confirm then adds a version to it). `proposed_load_changes` is what the
    draft *would* apply; it is not in `decisions.load_changes` until confirmed (A§4.3)."""

    decision_id: int
    output: Plan | Refusal
    guards_fired: list[GuardVerdict]
    proposed_load_changes: list[LoadChange]
    plan_id: int | None
    language: str

    @property
    def plan(self) -> Plan | None:
        return self.output if isinstance(self.output, Plan) else None

    @property
    def refusal(self) -> Refusal | None:
        return self.output if isinstance(self.output, Refusal) else None


@dataclass(frozen=True, slots=True)
class DraftBase:
    """Revise the draft held by this plan-round decision (a `Confirm`-able one)."""

    decision_id: int


@dataclass(frozen=True, slots=True)
class PlanBase:
    """Revise an existing plan (its latest version)."""

    plan_id: int


RevisionBase = DraftBase | PlanBase


class ConfirmStatus(StrEnum):
    SAVED = "saved"
    ALREADY_SAVED = "already_saved"  # idempotent double-tap: nothing new was created
    REFUSED = "refused"  # the re-validation failed (state changed since the draft)
    STALE = "stale"  # a newer round exists; this draft is no longer current
    NOT_FOUND = "not_found"  # no such draft for this user


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    status: ConfirmStatus
    plan_id: int | None = None
    plan_version_id: int | None = None
    version: int | None = None
    is_default: bool = False
    refusal: Refusal | None = None
    # The `plan_confirm` decision when SAVED; the `refusal` decision when REFUSED.
    decision_id: int | None = None


@dataclass(frozen=True, slots=True)
class PlanDetail:
    record: PlanRecord
    version: PlanVersionRecord
    plan: Plan


class PlanNotFoundError(LookupError):
    """The revision base (a draft decision or a plan) doesn't exist or isn't this user's."""


class StaleDraftError(PlanNotFoundError):
    """The draft decision exists but a newer round has superseded it."""


# --- Snapshot (one read unit) -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CompleteProfile:
    age_bucket: AgeBucket
    weight_bucket: WeightBucket
    experience: Experience
    barbell_experience: BarbellExperience
    preferences: list[Preference]
    focus: Focus
    location: Location
    equipment: frozenset[Equipment]
    sessions_per_week: int
    session_minutes: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    language: str
    profile: _CompleteProfile | None  # None: setup not finished
    flags: list[ScreeningFlagState]
    holds: list[HealthHoldReason]
    history_max: dict[str, float]
    outcomes: dict[str, list[SessionOutcome]]
    checkins: dict[str, CheckinAnswer]
    increases_7d: dict[str, list[float]]
    applied_to_kg_7d: dict[str, float]  # A§7: applied this week, not counted twice
    active_plan: Plan | None  # the default plan's latest version, if any


def _since_7d() -> str:
    return clock.format_timestamp(clock.now() - timedelta(days=7))


async def read_snapshot(conn: Connection, user_id: int) -> Snapshot:
    """Everything the gates, the guards and the LLM context need, off one connection (so it
    can run inside `db.read()` for a proposal and inside `db.transaction()` for a confirm)."""
    user = await get_user(conn, user_id)
    if user is None:
        raise PlanNotFoundError(f"no user {user_id}")
    profile_record = await get_profile(conn, user_id)
    flags = [
        ScreeningFlagState(
            flag=ScreeningFlag(record.flag),
            value=record.value,
            clearance=record.clearance,
        )
        for record in await list_screening_flags(conn, user_id)
    ]
    holds = [HealthHoldReason(hold.reason) for hold in await list_open_health_holds(conn, user_id)]
    history_max = await historical_max_by_exercise(conn, user_id)
    outcomes = await recent_session_outcomes_by_exercise(conn, user_id, limit=2)
    checkins = await latest_checkin_answers(conn, user_id)
    since_7d = _since_7d()
    increases = await recent_increase_deltas_by_exercise(conn, user_id, since=since_7d)
    applied = await applied_to_kg_by_exercise(conn, user_id, since=since_7d)

    active_plan: Plan | None = None
    default_plan = await get_default_plan(conn, user_id)
    if default_plan is not None and default_plan.status == _PLAN_STATUS_ACTIVE:
        latest = await get_latest_plan_version(conn, default_plan.id)
        if latest is not None:
            try:
                active_plan = Plan.model_validate(latest.body)
            except ValueError:
                # A stored version that no longer parses as a `Plan` (e.g. after a domain
                # model change) contributes no current loads; the reference then falls back
                # to the historical max, which only makes the increase checks stricter.
                _logger.warning(
                    "plan version %s does not parse as a Plan; ignoring for current loads",
                    latest.id,
                )

    profile: _CompleteProfile | None = None
    if (
        profile_record is not None
        and profile_record.completed_at is not None
        and profile_record.age_bucket is not None
        and profile_record.weight_bucket is not None
        and profile_record.experience is not None
        and profile_record.barbell_experience is not None
        and profile_record.focus is not None
        and profile_record.location is not None
        and profile_record.sessions_per_week is not None
        and profile_record.session_minutes is not None
    ):
        location = Location(profile_record.location)
        equipment = (
            frozenset(Equipment(item) for item in profile_record.equipment)
            if location == Location.HOME_EQUIPMENT
            else LOCATION_DEFAULT_EQUIPMENT[location]
        )
        profile = _CompleteProfile(
            age_bucket=AgeBucket(profile_record.age_bucket),
            weight_bucket=WeightBucket(profile_record.weight_bucket),
            experience=Experience(profile_record.experience),
            barbell_experience=BarbellExperience(profile_record.barbell_experience),
            preferences=[Preference(item) for item in profile_record.preferences],
            focus=Focus(profile_record.focus),
            location=location,
            equipment=equipment,
            sessions_per_week=profile_record.sessions_per_week,
            session_minutes=profile_record.session_minutes,
        )

    return Snapshot(
        language=user.language,
        profile=profile,
        flags=flags,
        holds=holds,
        history_max=history_max,
        outcomes=outcomes,
        checkins=checkins,
        increases_7d=increases,
        applied_to_kg_7d=applied,
        active_plan=active_plan,
    )


# --- Gates ------------------------------------------------------------------------------------


def refusal_for(code: RefusalCode, lang: str) -> Refusal:
    return Refusal(code=code, message=i18n.t(f"refusal.{code.value}", lang))


def gate(snapshot: Snapshot) -> tuple[RefusalCode, GuardVerdict] | None:
    """A§6.4 step 1, before any LLM call. `None` means every gate passed."""
    if snapshot.profile is None:
        return RefusalCode.PROFILE_INCOMPLETE, GuardVerdict(
            rule=_PROFILE_RULE, ok=False, detail="setup is not complete"
        )
    verdict = plan_allowed(snapshot.flags, snapshot.holds)
    if verdict.ok:
        return None
    if snapshot.holds:
        return RefusalCode.OPEN_HEALTH_HOLD, verdict
    if verdict.rule == _SCREENING_INCOMPLETE_RULE:
        return RefusalCode.SCREENING_INCOMPLETE, verdict
    return RefusalCode.NEEDS_CLEARANCE, verdict


# --- Guard context + LLM context ---------------------------------------------------------------


def _current_loads(snapshot: Snapshot) -> dict[str, float]:
    """A§7 current working load: the prescribed load of the exercise's last completed
    session, falling back to the active plan version's prescription (the lowest one, if the
    plan lists the exercise more than once: the conservative reference)."""
    current: dict[str, float] = {}
    if snapshot.active_plan is not None:
        for workout in snapshot.active_plan.workouts:
            for block in workout.blocks:
                for prescription in block.items:
                    kg = prescription.load.kg
                    if kg is None:
                        continue
                    known = current.get(prescription.exercise_id)
                    current[prescription.exercise_id] = kg if known is None else min(known, kg)
    for exercise_id, outcomes in snapshot.outcomes.items():
        if outcomes and outcomes[0].planned_load_kg is not None:
            current[exercise_id] = outcomes[0].planned_load_kg
    return current


def _weekly_caps(catalog: Catalog, settings: Settings) -> dict[str, float]:
    """Per-exercise weekly cap: the configured default, never above the catalog's own
    `increment_kg` (AGENTS.md §2: "catalog can set lower")."""
    default = settings.max_weekly_increment_kg
    return {exercise.id: min(default, exercise.increment_kg) for exercise in catalog.exercise}


@dataclass(frozen=True, slots=True)
class Inputs:
    """Everything one round needs after the snapshot: the guard context, the pure load-engine
    inputs (for A§7.3 substitution), and the pseudonymized LLM context."""

    catalog: Catalog
    ctx: GuardContext
    snapshot: Snapshot
    user_context: UserContext


def _history_summaries(snapshot: Snapshot) -> list[ExerciseHistorySummary]:
    exercise_ids = sorted(set(snapshot.history_max) | set(snapshot.outcomes))
    summaries: list[ExerciseHistorySummary] = []
    for exercise_id in exercise_ids:
        outcomes = snapshot.outcomes.get(exercise_id, [])
        last = outcomes[0] if outcomes else None
        last_reps = None if last is None else last.min_reps_performed
        summaries.append(
            ExerciseHistorySummary(
                exercise_id=exercise_id,
                last_working_load_kg=None if last is None else last.load_kg,
                # The summary field caps at 100 (a long conditioning set can exceed it).
                last_reps=None if last_reps is None else min(last_reps, _MAX_SUMMARY_REPS),
                historical_max_kg=snapshot.history_max.get(exercise_id),
            )
        )
    return summaries


def build_inputs(catalog: Catalog, snapshot: Snapshot, settings: Settings, user_id: int) -> Inputs:
    profile = snapshot.profile
    assert profile is not None  # the gate ran first
    ctx = GuardContext(
        catalog=catalog,
        flags=snapshot.flags,
        equipment=profile.equipment,
        location=profile.location,
        sessions_per_week=profile.sessions_per_week,
        holds=snapshot.holds,
        history_max_kg=snapshot.history_max,
        current_load_kg=_current_loads(snapshot),
        increases_7d=snapshot.increases_7d,
        applied_to_kg_7d=snapshot.applied_to_kg_7d,
        checkins=snapshot.checkins,
        weekly_cap_kg=_weekly_caps(catalog, settings),
        default_weekly_cap_kg=settings.max_weekly_increment_kg,
    )
    allowed = available_exercises(catalog, profile.location, profile.equipment, snapshot.flags)
    user_context = build_user_context(
        user_id=user_id,
        language=snapshot.language,
        age_bucket=profile.age_bucket,
        weight_bucket=profile.weight_bucket,
        experience=profile.experience,
        barbell_experience=profile.barbell_experience,
        preferences=profile.preferences,
        focus=profile.focus,
        location=profile.location,
        equipment=sorted(profile.equipment, key=lambda item: item.value),
        sessions_per_week=profile.sessions_per_week,
        session_minutes=profile.session_minutes,
        flags=[FlagSummary(flag=state.flag, value=state.value) for state in snapshot.flags],
        allowed_exercise_ids=[exercise.id for exercise in allowed],
        history=_history_summaries(snapshot),
    )
    return Inputs(catalog=catalog, ctx=ctx, snapshot=snapshot, user_context=user_context)


# --- Judging (pure) ---------------------------------------------------------------------------


@dataclass(slots=True)
class Judgement:
    verdicts: list[GuardVerdict]  # the final `validate_plan` verdicts (after substitution)
    fired: list[GuardVerdict]  # what `decisions.guards_fired` records

    @property
    def ok(self) -> bool:
        return all(verdict.ok for verdict in self.verdicts)

    @property
    def failures(self) -> list[GuardVerdict]:
        return [verdict for verdict in self.verdicts if not verdict.ok]


def _load_text(load: Load) -> str:
    return f"{load.kg:g} kg" if load.kind == "kg" and load.kg is not None else load.kind


def engine_load(exercise: Exercise, inputs: Inputs) -> tuple[Load, str, list[GuardVerdict]]:
    """The load engine's value for `exercise` (A§7.3): `calibration` with no completed
    session, otherwise deterministic double progression from the last prescribed load."""
    decision = engine_decision(exercise, inputs)
    return decision.load, decision.reason, decision.guards_fired


def engine_decision(exercise: Exercise, inputs: Inputs) -> LoadDecision:
    """`engine_load` with the engine's full structured decision (M8: the recap reads `kind`
    and `blocked_by`)."""
    history = ExerciseHistory(
        history_max_kg=inputs.snapshot.history_max.get(exercise.id),
        sessions=inputs.snapshot.outcomes.get(exercise.id, []),
    )
    decision = next_load(
        exercise,
        history,
        inputs.snapshot.checkins,
        inputs.snapshot.increases_7d.get(exercise.id, ()),
        inputs.ctx.weekly_cap_kg.get(exercise.id, inputs.ctx.default_weekly_cap_kg),
        flagged_areas=inputs.ctx.flagged_areas,
        applied_to_kg_7d=inputs.snapshot.applied_to_kg_7d.get(exercise.id),
    )
    return decision


def _sanitize_wording(plan: Plan, lang: str) -> list[GuardVerdict]:
    """AGENTS.md §3 over model-authored display text, in place: a note with a banned term is
    dropped; a plan name or workout title with one is replaced by a neutral default. Returns
    one informational verdict per replacement (logged to `guards_fired`)."""
    fired: list[GuardVerdict] = []

    def note(what: str, term: str, action: str) -> None:
        fired.append(
            GuardVerdict(rule=_WORDING_RULE, ok=True, detail=f"{what}: {term!r} found; {action}")
        )

    term = wording.first_forbidden_term(plan.name)
    if term is not None:
        plan.name = i18n.t("plan.default_name", lang)
        note("plan name", term, "replaced with the default name")
    for workout in plan.workouts:
        term = wording.first_forbidden_term(workout.title)
        if term is not None:
            workout.title = i18n.t("plan.default_workout_title", lang, workout_key=workout.key)
            note(f"workout {workout.key} title", term, "replaced with the default title")
        for block in workout.blocks:
            for prescription in block.items:
                if prescription.note is None:
                    continue
                term = wording.first_forbidden_term(prescription.note)
                if term is not None:
                    prescription.note = None
                    note(f"{prescription.exercise_id} note", term, "dropped")
    return fired


def judge(plan: Plan, inputs: Inputs) -> Judgement:
    """`validate_plan`, then A§7.3 load substitution when the only failures are load rules.
    **Mutates `plan` in place** when it substitutes a load: the object the LLM returned is
    what the caller shows, logs and (later) confirms, so the repair has to land on it — and
    `llm.escalation`'s `guard_check` contract only lets a judge return verdicts. The same
    pass sanitizes the model's display text (`_sanitize_wording`)."""
    return _judge_with(plan, lambda: validate_plan(plan, inputs.ctx), inputs)


def judge_workout(workout: Workout, inputs: Inputs) -> Judgement:
    """A§6.5.1: the same judging as `judge` — wording, guards, load substitution in place —
    over *one* workout with `guards.plan.validate_workout` (the gate and every
    per-prescription verdict, not the plan-wide schedule rules). Used by `/train` for the
    review, Start and Save to plan."""
    wrapper = Plan(
        name=i18n.t("plan.default_name", inputs.snapshot.language), schedule=[], workouts=[workout]
    )
    return _judge_with(wrapper, lambda: validate_workout(workout, inputs.ctx), inputs)


def _judge_with(
    plan: Plan, validate: Callable[[], list[GuardVerdict]], inputs: Inputs
) -> Judgement:
    fired: list[GuardVerdict] = _sanitize_wording(plan, inputs.snapshot.language)
    verdicts = validate()
    gate_verdicts = [verdict for verdict in verdicts if verdict.rule == "screening.plan_allowed"]
    failures = [verdict for verdict in verdicts if not verdict.ok]
    fired.extend([*gate_verdicts, *failures])
    if not failures or any(verdict.rule not in LOAD_RULES for verdict in failures):
        return Judgement(verdicts=verdicts, fired=fired)

    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                exercise = inputs.catalog.by_id(prescription.exercise_id)
                if exercise is None:
                    continue  # unreachable: a non-catalog id is a structural failure above
                if all(v.ok for v in load_verdicts(exercise, prescription.load, inputs.ctx)):
                    continue
                proposed = prescription.load
                replacement, reason, engine_verdicts = engine_load(exercise, inputs)
                fired.append(
                    GuardVerdict(
                        rule=_SUBSTITUTION_RULE,
                        ok=True,
                        detail=(
                            f"{exercise.id}: proposed {_load_text(proposed)} rejected; load "
                            f"engine value {_load_text(replacement)} used ({reason})"
                        ),
                    )
                )
                fired.extend(engine_verdicts)
                prescription.load = replacement

    verdicts = validate()
    fired.extend(verdict for verdict in verdicts if not verdict.ok)
    return Judgement(verdicts=verdicts, fired=fired)


def _reference_kg(ctx: GuardContext, exercise_id: str) -> float | None:
    """The guard's own reference (A§7, incl. the applied-this-week lift), so a kept confirmed
    load writes no `LoadChange`."""
    return reference_load_kg(ctx, exercise_id)


def load_changes_for(plan: Plan, ctx: GuardContext) -> list[LoadChange]:
    """Every prescribed kg above the reference load (A§7: `min(current, history_max)`), one
    entry per exercise (the highest prescribed load wins), for `decisions.load_changes`."""
    highest: dict[str, float] = {}
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                kg = prescription.load.kg
                if kg is None:
                    continue
                known = highest.get(prescription.exercise_id)
                highest[prescription.exercise_id] = kg if known is None else max(known, kg)
    changes: list[LoadChange] = []
    for exercise_id, kg in sorted(highest.items()):
        reference = _reference_kg(ctx, exercise_id)
        if reference is not None and kg > reference:
            changes.append(LoadChange(exercise_id=exercise_id, from_kg=reference, to_kg=kg))
    return changes


# --- Decision logging --------------------------------------------------------------------------


def draft_proposal(plan: Plan, proposed_load_changes: Sequence[LoadChange]) -> dict[str, object]:
    """`decisions.proposal` for a draft: the plan, plus the load changes confirming it would
    apply (A§4.3: shown here, counted only by the `plan_confirm` decision)."""
    return {
        "plan": plan.model_dump(mode="json"),
        "proposed_load_changes": [
            change.model_dump(mode="json") for change in proposed_load_changes
        ],
    }


def refusal_proposal(refusal: Refusal, rejected_plan: Plan | None = None) -> dict[str, object]:
    """`decisions.proposal` for a refused round: the refusal, plus the LLM's rejected plan
    when the guards (not the model) refused, so the log shows what was turned down. Never
    parses as a `Plan`, so `confirm_plan` can't mistake it for a draft."""
    proposal: dict[str, object] = {"refusal": refusal.model_dump(mode="json")}
    if rejected_plan is not None:
        proposal["rejected_plan"] = rejected_plan.model_dump(mode="json")
    return proposal


async def write_refusal_decision(
    conn: Connection,
    *,
    user_id: int,
    refusal: Refusal,
    guards_fired: Sequence[GuardVerdict],
    user_report: dict[str, object] | None,
    rejected_plan: Plan | None = None,
) -> int:
    return await insert_decision(
        conn,
        user_id=user_id,
        kind=DecisionKind.REFUSAL.value,
        prompt_template=None,
        prompt_version=None,
        model=None,
        content_version=content_version(),
        llm_input=None,
        user_report=user_report,
        proposal=refusal_proposal(refusal, rejected_plan),
        guards_fired=[verdict.model_dump() for verdict in guards_fired],
    )


async def _record_refusal(
    db: Database,
    *,
    user_id: int,
    refusal: Refusal,
    guards_fired: Sequence[GuardVerdict],
    user_report: dict[str, object] | None,
    plan_id: int | None,
    lang: str,
    rejected_plan: Plan | None = None,
) -> PlanRoundResult:
    async with db.transaction() as conn:
        decision_id = await write_refusal_decision(
            conn,
            user_id=user_id,
            refusal=refusal,
            guards_fired=guards_fired,
            user_report=user_report,
            rejected_plan=rejected_plan,
        )
    return PlanRoundResult(
        decision_id=decision_id,
        output=refusal,
        guards_fired=list(guards_fired),
        proposed_load_changes=[],
        plan_id=plan_id,
        language=lang,
    )


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One LLM attempt, judged and logged as its own decision (A§6.4: "every attempt is
    logged"). `judgement` is `None` when the model itself returned a `Refusal`."""

    decision_id: int
    outcome: AgentRunOutcome[Plan | Refusal]
    judgement: Judgement | None
    proposed_load_changes: list[LoadChange]


async def _log_attempt(
    db: Database,
    *,
    user_id: int,
    kind: DecisionKind,
    outcome: AgentRunOutcome[Plan | Refusal],
    llm_input: dict[str, object] | None,
    judgement: Judgement | None,
    user_report: dict[str, object] | None,
    ctx: GuardContext,
) -> _Attempt:
    """Write the attempt's `decisions` row: model/template from the run outcome, `llm_input`
    verbatim, the proposal (the judged — possibly load-substituted — plan with its proposed
    load changes, or the model's refusal) and `guards_fired`. `load_changes` stays `[]` on a
    draft (A§4.3: counted once, by the `plan_confirm` decision)."""
    output = outcome.output
    proposed: list[LoadChange] = []
    if isinstance(output, Refusal):
        proposal = refusal_proposal(output)
        guards_fired: list[GuardVerdict] = []
    else:
        assert judgement is not None
        guards_fired = judgement.fired
        if judgement.ok:
            proposed = load_changes_for(output, ctx)
        proposal = draft_proposal(output, proposed)
    prompt = outcome.prompt
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=kind.value,
            prompt_template=None if prompt is None else prompt.template_name,
            prompt_version=None if prompt is None else str(prompt.version),
            model=outcome.record.model,
            content_version=content_version(),
            llm_input=llm_input,
            user_report=user_report,
            proposal=proposal,
            guards_fired=[verdict.model_dump() for verdict in guards_fired],
            load_changes=[],
        )
    return _Attempt(
        decision_id=decision_id,
        outcome=outcome,
        judgement=judgement,
        proposed_load_changes=proposed,
    )


# --- Propose ---------------------------------------------------------------------------------


async def _gated_snapshot(
    db: Database, user_id: int
) -> tuple[Snapshot, tuple[RefusalCode, GuardVerdict] | None]:
    async with db.read() as conn:
        snapshot = await read_snapshot(conn, user_id)
    return snapshot, gate(snapshot)


def _no_allowed_exercises(inputs: Inputs) -> GuardVerdict | None:
    if inputs.user_context.allowed_exercise_ids:
        return None
    return GuardVerdict(
        rule=_ALLOWED_EXERCISES_RULE,
        ok=False,
        detail="no catalog exercise fits this location, equipment and screening",
    )


async def propose_new_plan(db: Database, llm: LlmRuntime, user_id: int) -> PlanRoundResult:
    """A§6.4: gates → context → `plan_generate` → guards (one feedback retry) → a draft or a
    `Refusal`. See the module docstring."""
    snapshot, gate_failure = await _gated_snapshot(db, user_id)
    lang = snapshot.language
    if gate_failure is not None:
        code, verdict = gate_failure
        return await _record_refusal(
            db,
            user_id=user_id,
            refusal=refusal_for(code, lang),
            guards_fired=[verdict],
            user_report=None,
            plan_id=None,
            lang=lang,
        )

    inputs = build_inputs(load_catalog(), snapshot, llm.settings, user_id)
    empty = _no_allowed_exercises(inputs)
    if empty is not None:
        return await _record_refusal(
            db,
            user_id=user_id,
            refusal=refusal_for(RefusalCode.NO_SAFE_EXERCISES, lang),
            guards_fired=[empty],
            user_report=None,
            plan_id=None,
            lang=lang,
        )

    spec = model_for("plan_generate", llm.settings)
    feedback: list[str] | None = None
    last: _Attempt | None = None
    for _round in range(_GENERATE_ATTEMPTS):
        rendered = render_user_prompt(inputs.user_context, guard_feedback=feedback)
        outcome = await run_agent(
            llm.factory("plan_generate"),
            spec.model,
            rendered.text,
            purpose="plan_generate",
            model_name=spec.model,
            prices=llm.prices,
            language=lang,
        )
        judgement = None if isinstance(outcome.output, Refusal) else judge(outcome.output, inputs)
        last = await _log_attempt(
            db,
            user_id=user_id,
            kind=DecisionKind.PLAN_GENERATE,
            outcome=outcome,
            llm_input=rendered.payload,
            judgement=judgement,
            user_report=None,
            ctx=inputs.ctx,
        )
        await record_llm_call(db, decision_id=last.decision_id, record=outcome.record)
        if judgement is None or judgement.ok:
            return PlanRoundResult(
                decision_id=last.decision_id,
                output=outcome.output,
                guards_fired=[] if judgement is None else judgement.fired,
                proposed_load_changes=last.proposed_load_changes,
                plan_id=None,
                language=lang,
            )
        feedback = [verdict.detail for verdict in judgement.failures]

    assert last is not None and last.judgement is not None
    rejected = last.outcome.output
    return await _record_refusal(
        db,
        user_id=user_id,
        refusal=refusal_for(RefusalCode.NO_SAFE_PLAN, lang),
        guards_fired=last.judgement.failures,
        user_report=None,
        plan_id=None,
        lang=lang,
        rejected_plan=rejected if isinstance(rejected, Plan) else None,
    )


# --- Revise ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Base:
    plan: Plan
    plan_id: int | None
    report: dict[str, object]  # `decisions.user_report` for every decision of this round


def _draft_plan(decision: DecisionRecord) -> Plan | None:
    """The `Plan` a plan-round decision proposed, or `None` if it holds a refusal."""
    if DecisionKind(decision.kind) not in _DRAFT_KINDS or decision.proposal is None:
        return None
    body = decision.proposal.get("plan")
    if body is None:
        return None
    try:
        return Plan.model_validate(body)
    except ValueError:
        return None


def _plan_id_of(decision: DecisionRecord) -> int | None:
    report = decision.user_report or {}
    plan_id = report.get("plan_id")
    return plan_id if isinstance(plan_id, int) else None


async def _resolve_base(conn: Connection, user_id: int, base: RevisionBase) -> _Base:
    if isinstance(base, DraftBase):
        decision = await get_decision(conn, base.decision_id)
        if decision is None or decision.user_id != user_id:
            raise PlanNotFoundError(f"no draft decision {base.decision_id} for this user")
        plan = _draft_plan(decision)
        if plan is None:
            raise PlanNotFoundError(f"decision {base.decision_id} holds no draft plan")
        latest = await get_latest_plan_round_decision(conn, user_id)
        if latest is None or latest.id != decision.id:
            raise StaleDraftError(f"draft {base.decision_id} was superseded by a newer round")
        plan_id = _plan_id_of(decision)
        report: dict[str, object] = {"base": "draft", "base_decision_id": decision.id}
        if plan_id is not None:
            report["plan_id"] = plan_id
        return _Base(plan=plan, plan_id=plan_id, report=report)

    record = await get_plan(conn, base.plan_id)
    if record is None or record.user_id != user_id:
        raise PlanNotFoundError(f"no plan {base.plan_id} for this user")
    version = await get_latest_plan_version(conn, record.id)
    if version is None:
        raise PlanNotFoundError(f"plan {base.plan_id} has no version")
    return _Base(
        plan=Plan.model_validate(version.body),
        plan_id=record.id,
        report={"base": "plan_version", "base_plan_version_id": version.id, "plan_id": record.id},
    )


async def revise_plan(
    db: Database, llm: LlmRuntime, user_id: int, base: RevisionBase, request_text: str
) -> PlanRoundResult:
    """A§6.4 step 6 with A§8.5 rule 3 escalation. `request_text` is the user's own words; it
    reaches the model only through `render_user_prompt(request=...)`, which scrubs it. The
    stop-word scan of that text is the caller's job, *before* this is called (A§6.3) — a hit
    halts and never gets here. Raises `PlanNotFoundError`/`StaleDraftError` for a bad `base`.
    """
    async with db.read() as conn:
        snapshot = await read_snapshot(conn, user_id)
        resolved = await _resolve_base(conn, user_id, base)
    lang = snapshot.language
    gate_failure = gate(snapshot)
    if gate_failure is not None:
        code, verdict = gate_failure
        return await _record_refusal(
            db,
            user_id=user_id,
            refusal=refusal_for(code, lang),
            guards_fired=[verdict],
            user_report=resolved.report,
            plan_id=resolved.plan_id,
            lang=lang,
        )

    inputs = build_inputs(load_catalog(), snapshot, llm.settings, user_id)
    empty = _no_allowed_exercises(inputs)
    if empty is not None:
        return await _record_refusal(
            db,
            user_id=user_id,
            refusal=refusal_for(RefusalCode.NO_SAFE_EXERCISES, lang),
            guards_fired=[empty],
            user_report=resolved.report,
            plan_id=resolved.plan_id,
            lang=lang,
        )

    payload_by_text: dict[str, dict[str, object]] = {}
    judgements: dict[int, Judgement] = {}
    attempts: list[_Attempt] = []

    def build_prompt(verdicts: Sequence[GuardVerdict] | None) -> str:
        feedback = (
            None if verdicts is None else [verdict.detail for verdict in verdicts if not verdict.ok]
        )
        rendered = render_user_prompt(
            inputs.user_context,
            request=request_text,
            current_plan=resolved.plan,
            guard_feedback=feedback,
        )
        payload_by_text[rendered.text] = rendered.payload
        return rendered.text

    def guard_check(plan: Plan) -> Sequence[GuardVerdict]:
        judgement = judge(plan, inputs)
        judgements[id(plan)] = judgement
        return judgement.verdicts

    async def on_attempt(
        outcome: AgentRunOutcome[Plan | Refusal], _verdicts: Sequence[GuardVerdict]
    ) -> int:
        prompt_text = None if outcome.prompt is None else outcome.prompt.user_prompt
        attempt = await _log_attempt(
            db,
            user_id=user_id,
            kind=DecisionKind.PLAN_REVISE,
            outcome=outcome,
            llm_input=None if prompt_text is None else payload_by_text.get(prompt_text),
            judgement=judgements.get(id(outcome.output)),
            user_report=resolved.report,
            ctx=inputs.ctx,
        )
        attempts.append(attempt)
        return attempt.decision_id

    escalation = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=llm.factory("plan_revise"),
        build_prompt=build_prompt,
        guard_check=guard_check,
        settings=llm.settings,
        db=db,
        prices=llm.prices,
        language=lang,
        on_attempt=on_attempt,
    )
    last = attempts[-1]
    if isinstance(escalation.output, Plan) or isinstance(last.outcome.output, Refusal):
        # A draft, or the model's own refusal: the last attempt's decision is the round's.
        judgement = last.judgement
        return PlanRoundResult(
            decision_id=last.decision_id,
            output=escalation.output,
            guards_fired=[] if judgement is None else judgement.fired,
            proposed_load_changes=last.proposed_load_changes,
            plan_id=resolved.plan_id,
            language=lang,
        )

    # Every attempt failed the guards: the escalation's own refusal, logged as its own
    # decision with the final failures (the attempts above each hold their rejected plan).
    assert last.judgement is not None
    rejected = last.outcome.output
    return await _record_refusal(
        db,
        user_id=user_id,
        refusal=escalation.output,
        guards_fired=last.judgement.failures,
        user_report=resolved.report,
        plan_id=resolved.plan_id,
        lang=lang,
        rejected_plan=rejected if isinstance(rejected, Plan) else None,
    )


# --- Confirm -----------------------------------------------------------------------------------


async def _confirmed_version(conn: Connection, draft_decision_id: int) -> PlanVersionRecord | None:
    """The plan version an earlier Confirm of this draft created, if any: the draft's
    `decision_outcomes` row records it (`plan_version_id`), which is what makes a double-tap
    idempotent."""
    for outcome in await list_decision_outcomes(conn, draft_decision_id):
        version_id = outcome.outcome.get("plan_version_id")
        if isinstance(version_id, int):
            return await get_plan_version(conn, version_id)
    return None


async def confirm_plan(
    db: Database, settings: Settings, user_id: int, decision_id: int
) -> ConfirmResult:
    """A§6.4 step 7, in ONE transaction (no network inside): load the draft, reject a stale or
    already-confirmed one, re-validate against a fresh `GuardContext`, then write `plans` +
    `decision(kind=plan_confirm)` (carrying the applied `load_changes`, A§4.3), `plans` +
    `plan_versions` (version 1 of a new plan, or n+1 of the plan the draft revises; linked to
    the confirm decision) and a `decision_outcome` on both decisions saying the user
    confirmed. Idempotent: a double-tap finds the version it already created
    (`ALREADY_SAVED`) before the staleness check (the confirm itself makes the draft stale)."""
    catalog = load_catalog()
    async with db.transaction() as conn:
        decision = await get_decision(conn, decision_id)
        if decision is None or decision.user_id != user_id:
            return ConfirmResult(status=ConfirmStatus.NOT_FOUND)
        plan = _draft_plan(decision)
        if plan is None:
            return ConfirmResult(status=ConfirmStatus.NOT_FOUND)

        already = await _confirmed_version(conn, decision_id)
        if already is not None:
            record = await get_plan(conn, already.plan_id)
            return ConfirmResult(
                status=ConfirmStatus.ALREADY_SAVED,
                plan_id=already.plan_id,
                plan_version_id=already.id,
                version=already.version,
                is_default=record is not None and record.is_default,
            )

        latest = await get_latest_plan_round_decision(conn, user_id)
        if latest is None or latest.id != decision_id:
            return ConfirmResult(status=ConfirmStatus.STALE)

        snapshot = await read_snapshot(conn, user_id)
        lang = snapshot.language
        gate_failure = gate(snapshot)
        if gate_failure is not None:
            code, verdict = gate_failure
            refusal = refusal_for(code, lang)
            refusal_id = await write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=[verdict],
                user_report={"confirm_of": decision_id},
                rejected_plan=plan,
            )
            return ConfirmResult(
                status=ConfirmStatus.REFUSED, refusal=refusal, decision_id=refusal_id
            )

        inputs = build_inputs(catalog, snapshot, settings, user_id)
        verdicts = validate_plan(plan, inputs.ctx)
        failures = [verdict for verdict in verdicts if not verdict.ok]
        if failures:
            refusal = refusal_for(RefusalCode.NO_SAFE_PLAN, lang)
            refusal_id = await write_refusal_decision(
                conn,
                user_id=user_id,
                refusal=refusal,
                guards_fired=failures,
                user_report={"confirm_of": decision_id},
                rejected_plan=plan,
            )
            return ConfirmResult(
                status=ConfirmStatus.REFUSED, refusal=refusal, decision_id=refusal_id
            )

        plan_id = _plan_id_of(decision)
        if plan_id is not None:
            record = await get_plan(conn, plan_id)
            if record is None or record.user_id != user_id:
                return ConfirmResult(status=ConfirmStatus.NOT_FOUND)
            previous = await get_latest_plan_version(conn, plan_id)
            version = 1 if previous is None else previous.version + 1
            is_default = record.is_default
        else:
            plans = await list_plans_for_user(conn, user_id)
            is_default = not any(
                item.is_default and item.status == _PLAN_STATUS_ACTIVE for item in plans
            )
            plan_id = await insert_plan(
                conn,
                user_id=user_id,
                name=plan.name,
                is_default=is_default,
                status=_PLAN_STATUS_ACTIVE,
            )
            version = 1
        # A§4.3 "load changes count once, when applied": the confirm decision carries them,
        # computed against the confirm-time reference; the draft carried none.
        load_changes = load_changes_for(plan, inputs.ctx)
        confirm_decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.PLAN_CONFIRM.value,
            prompt_template=decision.prompt_template,
            prompt_version=decision.prompt_version,
            model=decision.model,
            content_version=content_version(),
            llm_input=None,
            user_report={"draft_decision_id": decision_id, "plan_id": plan_id},
            proposal=draft_proposal(plan, load_changes),
            guards_fired=[verdict.model_dump() for verdict in verdicts if not verdict.ok],
            load_changes=load_changes,
        )
        plan_version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=version,
            body=plan.model_dump(mode="json"),
            origin=_ORIGIN_LLM,
            decision_id=confirm_decision_id,
        )
        outcome: dict[str, object] = {
            "confirmed": True,
            "plan_id": plan_id,
            "plan_version_id": plan_version_id,
            "version": version,
            "confirm_decision_id": confirm_decision_id,
        }
        await insert_decision_outcome(conn, decision_id=decision_id, outcome=outcome)
        await insert_decision_outcome(conn, decision_id=confirm_decision_id, outcome=outcome)
    return ConfirmResult(
        status=ConfirmStatus.SAVED,
        plan_id=plan_id,
        plan_version_id=plan_version_id,
        version=version,
        is_default=is_default,
        decision_id=confirm_decision_id,
    )


# --- Plan management ---------------------------------------------------------------------------


async def list_plans(db: Database, user_id: int) -> list[PlanRecord]:
    async with db.read() as conn:
        return await list_plans_for_user(conn, user_id)


async def get_plan_detail(db: Database, user_id: int, plan_id: int) -> PlanDetail | None:
    """The plan and its latest version, or `None` if it isn't this user's."""
    async with db.read() as conn:
        record = await get_plan(conn, plan_id)
        if record is None or record.user_id != user_id:
            return None
        version = await get_latest_plan_version(conn, plan_id)
    if version is None:
        return None
    return PlanDetail(record=record, version=version, plan=Plan.model_validate(version.body))


async def current_draft_id(db: Database, user_id: int) -> int | None:
    """The decision id of the user's current (latest-round) draft, or `None` if the latest
    round was a refusal or there is none. A button referencing any other decision is stale
    (A§6.3)."""
    async with db.read() as conn:
        latest = await get_latest_plan_round_decision(conn, user_id)
    if latest is None or _draft_plan(latest) is None:
        return None
    return latest.id


async def set_default(db: Database, user_id: int, plan_id: int) -> bool:
    """Make `plan_id` the one default (A§4.3). Only an active plan of this user's qualifies;
    returns `False` otherwise, changing nothing."""
    async with db.transaction() as conn:
        record = await get_plan(conn, plan_id)
        if record is None or record.user_id != user_id or record.status != _PLAN_STATUS_ACTIVE:
            return False
        await set_default_plan(conn, user_id, plan_id)
    return True


async def archive(db: Database, user_id: int, plan_id: int) -> bool:
    """Archive `plan_id`. An archived default hands the default to another active plan (the
    most recently created one), or to none. Returns `False` for a plan that isn't this
    user's or is already archived."""
    async with db.transaction() as conn:
        record = await get_plan(conn, plan_id)
        if record is None or record.user_id != user_id or record.status == _PLAN_STATUS_ARCHIVED:
            return False
        await update_plan_status(conn, plan_id, _PLAN_STATUS_ARCHIVED)
        if record.is_default:
            await clear_default_plan(conn, user_id, plan_id)
            others = [
                item
                for item in await list_plans_for_user(conn, user_id)
                if item.id != plan_id and item.status == _PLAN_STATUS_ACTIVE
            ]
            if others:
                await set_default_plan(conn, user_id, others[-1].id)
    return True


__all__ = [
    "ConfirmResult",
    "ConfirmStatus",
    "DraftBase",
    "Inputs",
    "Judgement",
    "PlanBase",
    "PlanDetail",
    "PlanNotFoundError",
    "PlanRoundResult",
    "RevisionBase",
    "Snapshot",
    "StaleDraftError",
    "archive",
    "build_inputs",
    "confirm_plan",
    "current_draft_id",
    "draft_proposal",
    "engine_decision",
    "engine_load",
    "gate",
    "get_plan_detail",
    "judge",
    "judge_workout",
    "list_plans",
    "load_changes_for",
    "propose_new_plan",
    "read_snapshot",
    "refusal_for",
    "refusal_proposal",
    "revise_plan",
    "set_default",
    "write_refusal_decision",
]
