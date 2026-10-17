"""Pure text/keyboard builders for `/train` (A§6.5): the workout suggestion, the precheck,
the review with explicit engine loads, one block at a time with an explicit load on every
set, and what the assistant logged. Nothing here touches the database or the LLM.

Every in-workout keyboard carries the persistent ⚠ Pain / feeling unwell button (A§6.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote_plus

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from fitme.bot.callback_data import CheckinReply, TrainAction, TrainPick
from fitme.bot.plan_rendering import (
    exercise_name,
    reps_text,
    rest_text,
    target_text,
    workout_block_lines,
)
from fitme.db.records import CheckinRecord, PlanRecord
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import BlockRole, CheckinAnswer, Measure
from fitme.domain.models import Block, Load, Prescription, Workout, is_activity_id
from fitme.domain.results import (
    ChangeReps,
    ChangeSets,
    ChangeTarget,
    PlanChange,
    SetResult,
    SwapExercise,
)
from fitme.i18n import t
from fitme.services import prescribing
from fitme.services.prescribing import load_label
from fitme.services.progression import TargetKind
from fitme.services.recap import NextKind, RecapView
from fitme.services.training import BlockView, LoggedItem, ReviewView


def _rows(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def _action(
    text: str,
    action: str,
    session_id: int,
    *,
    block: int = 0,
    item: int = 0,
    decision_id: int = 0,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=TrainAction(
            action=action, session_id=session_id, block=block, item=item, decision_id=decision_id
        ).pack(),
    )


def workout_label(workout: Workout) -> str:
    return f"{workout.key} — {workout.title}"


# --- Before a session exists --------------------------------------------------------------


def plan_choice_markup(plans: Sequence[PlanRecord]) -> InlineKeyboardMarkup:
    return _rows(
        *(
            InlineKeyboardButton(
                text=plan.name, callback_data=TrainPick(kind="plan", plan_id=plan.id).pack()
            )
            for plan in plans
        )
    )


def suggestion_text(plan: PlanRecord, workout: Workout, *, scheduled_today: bool, lang: str) -> str:
    key = "train.suggested_today" if scheduled_today else "train.suggested_next"
    return t(key, lang, plan=plan.name, workout=workout_label(workout))


def suggestion_markup(
    plan_id: int,
    workout: Workout,
    *,
    has_others: bool,
    has_other_plans: bool = False,
    lang: str,
) -> InlineKeyboardMarkup:
    """Start / Pick another (workout of the same plan) / Another plan (A§4.3: every plan can
    be trained from at any time, not only the default one)."""
    buttons = [
        InlineKeyboardButton(
            text=t("train.start_button", lang),
            callback_data=TrainPick(kind="workout", plan_id=plan_id, key=workout.key).pack(),
        )
    ]
    if has_others:
        buttons.append(
            InlineKeyboardButton(
                text=t("train.pick_another_button", lang),
                callback_data=TrainPick(kind="workouts", plan_id=plan_id).pack(),
            )
        )
    if has_other_plans:
        buttons.append(
            InlineKeyboardButton(
                text=t("train.other_plan_button", lang),
                callback_data=TrainPick(kind="plans", plan_id=0).pack(),
            )
        )
    return _rows(*buttons)


def workout_choice_markup(plan_id: int, workouts: Sequence[Workout]) -> InlineKeyboardMarkup:
    return _rows(
        *(
            InlineKeyboardButton(
                text=workout_label(workout),
                callback_data=TrainPick(kind="workout", plan_id=plan_id, key=workout.key).pack(),
            )
            for workout in workouts
        )
    )


# --- Precheck, resume, abort -----------------------------------------------------------------


def precheck_markup(session_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _action(t("precheck.no_button", lang), "precheck_no", session_id),
                _action(t("precheck.yes_button", lang), "precheck_yes", session_id),
            ]
        ]
    )


def resume_markup(session_id: int, lang: str) -> InlineKeyboardMarkup:
    return _rows(
        _action(t("train.resume_button", lang), "resume", session_id),
        _action(t("train.abort_button", lang), "abort", session_id),
        _action(t("workout.pain_button", lang), "pain", session_id),
    )


def abort_markup(session_id: int, lang: str) -> InlineKeyboardMarkup:
    return _rows(_action(t("train.abort_button", lang), "abort", session_id))


# --- Review -----------------------------------------------------------------------------------


def review_text(view: ReviewView, *, catalog: Catalog, lang: str) -> str:
    workout = view.workout
    lines = [t("train.review_title", lang, workout_key=workout.key, title=workout.title)]
    if view.adjusted:
        lines.append(t("train.review_adjusted", lang))
    lines.append("")
    lines.extend(workout_block_lines(workout.blocks, catalog, lang))
    lines.extend(["", t("disclosure.ai", lang)])
    return "\n".join(lines)


def review_markup(session_id: int, *, adjusted: bool, lang: str) -> InlineKeyboardMarkup:
    buttons = [
        _action(t("train.start_workout_button", lang), "start", session_id),
        _action(t("train.adjust_button", lang), "adjust", session_id),
    ]
    if adjusted:
        buttons.append(_action(t("train.save_to_plan_button", lang), "save_plan", session_id))
    buttons.append(_action(t("train.abort_button", lang), "abort", session_id))
    buttons.append(_action(t("workout.pain_button", lang), "pain", session_id))
    return _rows(*buttons)


# --- Blocks -----------------------------------------------------------------------------------


def block_text(view: BlockView, *, catalog: Catalog, lang: str) -> str:
    """A§6.5 step 5: name, sets × reps with an **explicit load on every set**, rest, and the
    vetted catalog instructions in the user's language."""
    lines = [t("train.block_title", lang, index=view.index + 1, total=view.total)]
    superset = view.block.kind == "superset"
    if superset:
        # ADR 0011: rounds and the one rest after each round up front; each exercise but the
        # last then says only its transition to the next.
        lines.append(
            t(
                "plan.superset_rounds_label",
                lang,
                rounds=max(item.sets for item in view.block.items),
                rest=rest_text(view.block.items[-1].rest_seconds, lang),
            )
        )
    last = len(view.block.items) - 1
    for index, item in enumerate(view.block.items):
        exercise = catalog.by_id(item.exercise_id)
        lines.append("")
        lines.append(
            exercise_name(exercise, item.exercise_id, lang, activity_name=item.activity_name)
        )
        # One prescription has one load for every set, so the sets are stated once ("4 × 6–8")
        # with that explicit load, instead of one identical line per set.
        # ADR 0016: a unilateral exercise's target is per side, here as in the plan view.
        per_side = exercise is not None and exercise.unilateral
        if item.measure == Measure.REPS:
            assert item.reps_min is not None and item.reps_max is not None
            sets_line = t(
                "train.sets_line_per_side" if per_side else "train.sets_line",
                lang,
                sets=item.sets,
                reps=reps_text(item.reps_min, item.reps_max),
            )
        else:
            target = target_text(item, lang)
            if per_side:
                target = t("plan.per_side", lang, target=target)
            sets_line = t("train.sets_line_target", lang, sets=item.sets, target=target)
        lines.append(sets_line)
        load = _block_load_label(item, exercise, lang, view.block.role)
        lines.append(t("train.load_line", lang, load=load))
        if not superset:
            lines.append(t("train.rest_line", lang, rest=rest_text(item.rest_seconds, lang)))
        elif index < last:
            if item.rest_seconds > 0:
                rest = rest_text(item.rest_seconds, lang)
                lines.append(t("train.transition_line", lang, rest=rest))
            else:
                lines.append(t("train.no_transition_line", lang))
        if item.note:
            lines.append(item.note)
        if exercise is not None:
            instructions = exercise.instructions.get(lang) or exercise.instructions.get("en")
            if instructions:
                lines.append(instructions)
        if item.load.kind == "calibration":
            lines.append(t("train.calibration_hint", lang))
    if not all_calibration(view.block):
        lines.extend(["", t("train.according_hint", lang)])
    return "\n".join(lines)


def _block_load_label(
    item: Prescription, exercise: Exercise | None, lang: str, role: BlockRole | None = None
) -> str:
    """The load as one short phrase for a workout block (ADR 0011, `services.prescribing`):
    how to pick a calibration load for this stimulus; how to log it is the calibration hint
    line below, said once instead of on every set."""
    return prescribing.prescription_load_label(item, exercise, lang, section="train", role=role)


def to_plan_markup(session_id: int, lang: str) -> InlineKeyboardMarkup:
    """ADR 0004: after a workout changed during the workout, offer to carry the changes
    into the plan (as a planning-session draft, saved like any other)."""
    return _rows(_action(t("train.to_plan_button", lang), "to_plan", session_id))


def all_calibration(block: Block) -> bool:
    """A§6.5.1: on a block where every item is a calibration load there is no weight to
    confirm, so ✅ is hidden and the weight used is written as a message (ADR 0013)."""
    return all(item.load.kind == "calibration" for item in block.items)


_YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="


def youtube_search_buttons(
    block: Block, *, catalog: Catalog, lang: str
) -> list[InlineKeyboardButton]:
    """One "search on YouTube" link per exercise of the block: the catalog's `search` term,
    or the name plus "technique", in the user's language (no user data), opened by the user's
    own device on tap — the server calls nothing. An https youtube.com link is what the
    YouTube app claims, so it opens there when installed (the Bot API allows no app-scheme
    URLs). A non-catalog activity item gets none."""
    buttons: list[InlineKeyboardButton] = []
    items = [item for item in block.items if not is_activity_id(item.exercise_id)]
    for item in items:
        exercise = catalog.by_id(item.exercise_id)
        name = exercise_name(exercise, item.exercise_id, lang)
        search = None if exercise is None else exercise.search.get(lang)
        query = search or t("workout.youtube_query", lang, name=name)
        text = (
            t("workout.youtube_button", lang)
            if len(items) == 1
            else t("workout.youtube_button_named", lang, name=name)
        )
        buttons.append(InlineKeyboardButton(text=text, url=_YOUTUBE_SEARCH + quote_plus(query)))
    return buttons


def block_markup(view: BlockView, *, catalog: Catalog, lang: str) -> InlineKeyboardMarkup:
    session_id, block = view.session_id, view.index
    buttons = youtube_search_buttons(view.block, catalog=catalog, lang=lang)
    if not all_calibration(view.block):
        buttons.append(
            _action(t("workout.according_to_plan_button", lang), "done", session_id, block=block)
        )
    buttons.extend(
        [
            _action(t("workout.skip_button", lang), "skip", session_id, block=block),
            _action(t("workout.pain_button", lang), "pain", session_id, block=block),
        ]
    )
    return _rows(*buttons)


# --- Logged results ---------------------------------------------------------------------------


def logged_item_text(item: LoggedItem, lang: str) -> str:
    """One exercise the assistant logged (ADR 0005), as written, on one line: "✓ Squat:
    4 × 9 @ 40 kg", "✓ Squat: 9, 9, 8, — @ 40 kg", "✓ Plank: 3 × 1 min"."""
    name = exercise_name(item.exercise, item.exercise_id, lang)
    return t("train.logged_line", lang, name=name, sets=_sets_text(item.sets, item.exercise, lang))


def _sets_text(sets: Sequence[SetResult], exercise: Exercise | None, lang: str) -> str:
    ordered = sorted(sets, key=lambda result: result.set_index)
    if all(result.skipped for result in ordered):
        return t("train.logged_all_skipped", lang)
    loads = {result.load_kg for result in ordered if not result.skipped}
    shared_load = next(iter(loads)) if len(loads) == 1 else None

    def amount(result: SetResult) -> str:
        if result.skipped:
            return t("train.logged_skipped", lang)
        # ADR 0009: a timed or distance set reports seconds or meters, not reps.
        if result.seconds is not None:
            text = rest_text(result.seconds, lang)
        elif result.meters is not None:
            text = t("duration.meters", lang, m=result.meters)
        else:
            text = str(result.reps)
        if shared_load is None and result.load_kg is not None:
            text = f"{text} @ {_kg_text(result.load_kg, exercise, lang)}"
        return text

    amounts = [amount(result) for result in ordered]
    text = (
        f"{len(amounts)} × {amounts[0]}"
        if len(amounts) > 1 and len(set(amounts)) == 1 and not ordered[0].skipped
        else ", ".join(amounts)
    )
    if shared_load is not None:
        text = f"{text} @ {_kg_text(shared_load, exercise, lang)}"
    return text


def _kg_text(kg: float, exercise: Exercise | None, lang: str) -> str:
    if exercise is not None and exercise.load_unit == "per_implement":
        return t("load.per_implement", lang, kg=f"{kg:g}")
    return t("plan.load_kg", lang, kg=f"{kg:g}")


# --- Recap (M8) --------------------------------------------------------------------------------


def _name(catalog: Catalog, exercise_id: str, lang: str) -> str:
    return exercise_name(catalog.by_id(exercise_id), exercise_id, lang)


def _load(catalog: Catalog, exercise_id: str, load: Load, lang: str) -> str:
    return load_label(load, catalog.by_id(exercise_id), lang)


def recap_text(view: RecapView, *, catalog: Catalog, lang: str) -> str:
    """The deterministic recap (numbers from the engine's preview, never the model's), then
    the model's text when it passed the wording check, then the disclosure."""
    proposal = view.proposal
    lines = [t("recap.title", lang, workout=workout_label(view.workout)), ""]
    for item in proposal.summary:
        name = _name(catalog, item.exercise_id, lang)
        if item.measure == Measure.TIME:
            line = t(
                "recap.exercise_line_target",
                lang,
                name=name,
                done=item.done_sets,
                planned=item.planned_sets,
                total=rest_text(item.total_seconds, lang),
            )
        elif item.measure == Measure.DISTANCE:
            line = t(
                "recap.exercise_line_target",
                lang,
                name=name,
                done=item.done_sets,
                planned=item.planned_sets,
                total=t("duration.meters", lang, m=item.total_meters),
            )
        else:
            line = t(
                "recap.exercise_line",
                lang,
                name=name,
                done=item.done_sets,
                planned=item.planned_sets,
                reps=item.total_reps,
            )
        if item.volume_kg > 0:
            line += t("recap.exercise_volume", lang, volume=f"{item.volume_kg:g}")
        if item.skipped_sets:
            line += t("recap.exercise_skipped", lang, skipped=item.skipped_sets)
        if item.new_max_kg is not None:
            line += t(
                "recap.new_max",
                lang,
                load=_load(catalog, item.exercise_id, Load(kind="kg", kg=item.new_max_kg), lang),
            )
        lines.append(line)
    # ADR 0016: the app's own target decisions that come with no suggestion to tap.
    target_lines = [
        t(f"recap.target_{target.kind.value}", lang, name=_name(catalog, target.exercise_id, lang))
        for target in proposal.targets
        if target.kind in (TargetKind.SPEED, TargetKind.VARIATION)
    ]
    if proposal.preview or target_lines:
        lines.extend(["", t("recap.next_title", lang)])
    lines.extend(target_lines)
    for step in proposal.preview:
        name = _name(catalog, step.exercise_id, lang)
        current = _load(catalog, step.exercise_id, step.current, lang)
        nxt = _load(catalog, step.exercise_id, step.next, lang)
        key = {
            NextKind.INCREASE: "recap.next_increase",
            NextKind.INCREASE_DID_MORE: "recap.next_increase_did_more",
            NextKind.HOLD: "recap.next_hold",
            NextKind.HOLD_BLOCKED: "recap.next_hold_blocked",
            NextKind.HOLD_CHECKIN: "recap.next_hold_checkin",
            NextKind.DECREASE: "recap.next_decrease",
            NextKind.CLAMPED: "recap.next_clamped",
            NextKind.CALIBRATION: "recap.next_calibration",
            NextKind.BODYWEIGHT: "recap.next_bodyweight",
            NextKind.HOLD_SPEED: "recap.next_hold_speed",
        }[step.kind]
        if_fine = (
            "" if step.if_fine is None else _load(catalog, step.exercise_id, step.if_fine, lang)
        )
        lines.append(t(key, lang, name=name, load=nxt, current=current, if_fine=if_fine))
    if proposal.recap_text:
        lines.extend(["", proposal.recap_text])
    lines.extend(["", t("disclosure.ai", lang)])
    return "\n".join(lines)


def checkin_question(checkin: CheckinRecord, lang: str) -> str:
    area = checkin.question_key.removeprefix("area:")
    return t("checkin.question", lang, area=t(f"enum.body_area.{area}", lang))


def checkin_markup(checkin: CheckinRecord, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(f"checkin.answer_{answer.value}", lang),
                    callback_data=CheckinReply(checkin_id=checkin.id, answer=answer.value).pack(),
                )
                for answer in (CheckinAnswer.FINE, CheckinAnswer.WORSE, CheckinAnswer.PAIN)
            ]
        ]
    )


def plan_change_text(change: PlanChange, *, catalog: Catalog, lang: str) -> str:
    if isinstance(change, SwapExercise):
        return t(
            "recap.suggestion_swap",
            lang,
            from_name=_name(catalog, change.from_exercise_id, lang),
            to_name=_name(catalog, change.to_exercise_id, lang),
        )
    if isinstance(change, ChangeSets):
        return t(
            "recap.suggestion_sets",
            lang,
            name=_name(catalog, change.exercise_id, lang),
            sets=change.sets,
        )
    if isinstance(change, ChangeTarget):
        if change.seconds is not None:
            target, current = rest_text(change.seconds, lang), rest_text(change.from_target, lang)
        else:
            target = t("duration.meters", lang, m=change.meters)
            current = t("duration.meters", lang, m=change.from_target)
        return t(
            "recap.suggestion_target",
            lang,
            name=_name(catalog, change.exercise_id, lang),
            target=target,
            current=current,
        )
    assert isinstance(change, ChangeReps)
    return t(
        "recap.suggestion_reps",
        lang,
        name=_name(catalog, change.exercise_id, lang),
        reps_min=change.reps_min,
        reps_max=change.reps_max,
    )


def plan_change_markup(view: RecapView, index: int, lang: str) -> InlineKeyboardMarkup:
    return _rows(
        _action(
            t("recap.apply_button", lang),
            "apply",
            view.session_id,
            item=index,
            decision_id=view.decision_id,
        )
    )
