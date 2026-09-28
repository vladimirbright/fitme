"""Pure text/keyboard builders for `/train` (A§6.5): the workout suggestion, the precheck,
the review with explicit engine loads, one block at a time with an explicit load on every
set, the result prompts and the parsed table. Nothing here touches the database or the LLM.

Every in-workout keyboard carries the persistent ⚠ Pain / feeling unwell button (A§6.3).
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from fitme.bot.callback_data import CheckinReply, TrainAction, TrainPick
from fitme.bot.plan_rendering import (
    exercise_name,
    load_label,
    prescription_line,
    prescription_load_label,
)
from fitme.db.records import CheckinRecord, PlanRecord
from fitme.domain.catalog import Catalog
from fitme.domain.enums import CheckinAnswer
from fitme.domain.models import Block, Load, Workout
from fitme.domain.results import ChangeReps, ParsedResults, PlanChange, SwapExercise
from fitme.i18n import t
from fitme.services.recap import NextKind, RecapView
from fitme.services.training import BlockView, ResultPrompt, ReviewView


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
    for block in workout.blocks:
        if block.kind == "superset":
            lines.append(t("plan.superset_label", lang))
            lines.extend(f"    • {prescription_line(item, catalog, lang)}" for item in block.items)
        else:
            lines.append(f"• {prescription_line(block.items[0], catalog, lang)}")
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
    if view.block.kind == "superset":
        lines.append(t("plan.superset_label", lang))
    for item in view.block.items:
        exercise = catalog.by_id(item.exercise_id)
        lines.append("")
        lines.append(exercise_name(exercise, item.exercise_id, lang))
        load = prescription_load_label(item, exercise, lang)  # M8b: the declared hint, if any
        for set_index in range(1, item.sets + 1):
            lines.append(
                t(
                    "train.set_line",
                    lang,
                    index=set_index,
                    reps_min=item.reps_min,
                    reps_max=item.reps_max,
                    load=load,
                )
            )
        lines.append(t("train.rest_line", lang, rest=item.rest_seconds))
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


def all_calibration(block: Block) -> bool:
    """A§6.5.1: on a block where every item is a calibration load there is no weight to
    confirm, so ✅ is hidden and the weight has to be logged through ✏️."""
    return all(item.load.kind == "calibration" for item in block.items)


def block_markup(view: BlockView, lang: str) -> InlineKeyboardMarkup:
    session_id, block = view.session_id, view.index
    buttons: list[InlineKeyboardButton] = []
    if not all_calibration(view.block):
        buttons.append(
            _action(t("workout.according_to_plan_button", lang), "done", session_id, block=block)
        )
    buttons.extend(
        [
            _action(t("workout.enter_results_button", lang), "enter", session_id, block=block),
            _action(t("workout.skip_button", lang), "skip", session_id, block=block),
            _action(t("workout.pain_button", lang), "pain", session_id, block=block),
        ]
    )
    return _rows(*buttons)


# --- Results ----------------------------------------------------------------------------------


def prompt_name(prompt: ResultPrompt, lang: str) -> str:
    return exercise_name(prompt.exercise, prompt.prescription.exercise_id, lang)


def results_prompt_text(prompt: ResultPrompt, lang: str) -> str:
    return t("train.results_prompt", lang, name=prompt_name(prompt, lang))


def results_unclear_text(prompt: ResultPrompt, lang: str) -> str:
    return t("train.results_unclear", lang, name=prompt_name(prompt, lang))


def results_implausible_text(prompt: ResultPrompt, lang: str) -> str:
    load = load_label(prompt.prescription.load, prompt.exercise, lang)
    return t("train.results_implausible", lang, name=prompt_name(prompt, lang), load=load)


def parsed_text(prompt: ResultPrompt, parsed: ParsedResults, lang: str) -> str:
    lines = [t("train.parsed_title", lang, name=prompt_name(prompt, lang))]
    for result in sorted(parsed.sets, key=lambda item: item.set_index):
        if result.skipped:
            lines.append(t("train.parsed_set_skipped", lang, index=result.set_index))
        elif result.load_kg is None:
            lines.append(
                t("train.parsed_set_no_load", lang, index=result.set_index, reps=result.reps)
            )
        else:
            kg = f"{result.load_kg:g}"
            load = (
                t("load.per_implement", lang, kg=kg)
                if prompt.exercise is not None and prompt.exercise.load_unit == "per_implement"
                else t("plan.load_kg", lang, kg=kg)
            )
            lines.append(
                t(
                    "train.parsed_set_line",
                    lang,
                    index=result.set_index,
                    reps=result.reps,
                    load=load,
                )
            )
    return "\n".join(lines)


def parsed_markup(prompt: ResultPrompt, decision_id: int, lang: str) -> InlineKeyboardMarkup:
    return _rows(
        _action(
            t("train.correct_button", lang),
            "parse_ok",
            prompt.session_id,
            block=prompt.block,
            item=prompt.item,
            decision_id=decision_id,
        ),
        _action(
            t("train.fix_button", lang),
            "parse_fix",
            prompt.session_id,
            block=prompt.block,
            item=prompt.item,
        ),
        _action(t("workout.pain_button", lang), "pain", prompt.session_id, block=prompt.block),
    )


def results_pending_markup(session_id: int, block: int, lang: str) -> InlineKeyboardMarkup:
    """Under the "send your results" prompt: the ⚠ button stays reachable."""
    return _rows(_action(t("workout.pain_button", lang), "pain", session_id, block=block))


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
    if proposal.preview:
        lines.extend(["", t("recap.next_title", lang)])
    for step in proposal.preview:
        name = _name(catalog, step.exercise_id, lang)
        current = _load(catalog, step.exercise_id, step.current, lang)
        nxt = _load(catalog, step.exercise_id, step.next, lang)
        key = {
            NextKind.INCREASE: "recap.next_increase",
            NextKind.HOLD: "recap.next_hold",
            NextKind.HOLD_BLOCKED: "recap.next_hold_blocked",
            NextKind.HOLD_CHECKIN: "recap.next_hold_checkin",
            NextKind.DECREASE: "recap.next_decrease",
            NextKind.CLAMPED: "recap.next_clamped",
            NextKind.CALIBRATION: "recap.next_calibration",
            NextKind.BODYWEIGHT: "recap.next_bodyweight",
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
