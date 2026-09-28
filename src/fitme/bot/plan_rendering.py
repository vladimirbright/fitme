"""Pure text/keyboard builders for `/plan` (A§6.4 step 5): a plan as structured text —
schedule with localized weekday names, then each workout with its exercises, sets × reps,
rest, and an **explicit load** on every prescription — plus the plan-list and draft
keyboards, and the Telegram message-length split.

Nothing here touches the database or the LLM: callers pass the loaded `Plan`, the catalog
(for localized exercise names and the calibration start hint) and the language.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitme.bot.callback_data import PlanDraft, PlanMenu
from fitme.db.records import PlanRecord
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.models import Load, Plan, Prescription
from fitme.i18n import t, wording

TELEGRAM_MESSAGE_LIMIT = 4096
_DEFAULT_MARK = "★"


def _kg_text(kg: float) -> str:
    return f"{kg:g}"


def exercise_name(exercise: Exercise | None, exercise_id: str, lang: str) -> str:
    if exercise is None:
        return exercise_id
    return exercise.names.get(lang) or exercise.names.get("en") or exercise_id


def load_label(load: Load, exercise: Exercise | None, lang: str) -> str:
    """The explicit load text (A§6.4 step 5): a kg number ("each" when the catalog
    `load_unit` is `per_implement`, A§4.4 "loads are per implement"; a single implement or a
    total is a plain kg figure), "bodyweight", or the calibration wording with the catalog
    start as the "start with X or lighter" hint (A§7.3: the engine never prescribes that
    start itself)."""
    if load.kind == "kg":
        assert load.kg is not None  # Load's own validator guarantees this
        if exercise is not None and exercise.load_unit == "per_implement":
            return t("load.per_implement", lang, kg=_kg_text(load.kg))
        return t("plan.load_kg", lang, kg=_kg_text(load.kg))
    if load.kind == "bodyweight":
        return t("plan.load_bodyweight", lang)
    if exercise is not None and exercise.start.kind == "kg":
        # A§7.3: the catalog kg start is a display hint only ("start with X or lighter").
        start = load_label(exercise.start, exercise, lang)
        return t("plan.load_calibration_from", lang, start=start)
    return t("plan.load_calibration", lang)


def prescription_load_label(
    prescription: Prescription, exercise: Exercise | None, lang: str
) -> str:
    """`load_label` for a prescription, with the M8b declared hint when the shown load is
    `calibration` and the user's pasted plan declared a kg for it ("calibration — your plan
    says 80 kg; start at or below it and log what you used"; "each" per `load_unit`). The
    hint is display only: the prescribed load is still calibration."""
    load = prescription.load
    if load.kind == "calibration" and prescription.declared_kg is not None:
        declared = load_label(Load(kind="kg", kg=prescription.declared_kg), exercise, lang)
        return t("plan.load_calibration_declared", lang, declared=declared)
    return load_label(load, exercise, lang)


def prescription_line(prescription: Prescription, catalog: Catalog, lang: str) -> str:
    exercise = catalog.by_id(prescription.exercise_id)
    sets_reps = t(
        "plan.sets_reps",
        lang,
        sets=prescription.sets,
        reps_min=prescription.reps_min,
        reps_max=prescription.reps_max,
    )
    line = t(
        "plan.prescription_line",
        lang,
        name=exercise_name(exercise, prescription.exercise_id, lang),
        sets_reps=sets_reps,
        load=prescription_load_label(prescription, exercise, lang),
        rest=prescription.rest_seconds,
    )
    if prescription.note:
        line = f"{line} — {prescription.note}"
    return line


def render_plan_text(
    plan: Plan, *, catalog: Catalog, lang: str, title: str, unmatched: Sequence[str] = ()
) -> str:
    """The whole plan as one text (split with `split_message` before sending): `title`, the
    schedule, every workout with supersets grouped, the "Not matched:" list of a pasted plan's
    exercises that map to no catalog id (M8b, when any), and the AI disclosure footer."""
    unmatched = displayable_unmatched(unmatched)
    lines: list[str] = [title, "", t("plan.schedule_title", lang)]
    workouts_by_key = {workout.key: workout for workout in plan.workouts}
    for day in sorted(plan.schedule, key=lambda item: item.weekday):
        workout = workouts_by_key.get(day.workout_key)
        label = day.workout_key if workout is None else f"{workout.key} — {workout.title}"
        lines.append(
            t("plan.schedule_line", lang, weekday=t(f"weekday.{day.weekday}", lang), workout=label)
        )
    for workout in plan.workouts:
        lines.append("")
        lines.append(t("plan.workout_title", lang, workout_key=workout.key, title=workout.title))
        for block in workout.blocks:
            if block.kind == "superset":
                lines.append(t("plan.superset_label", lang))
                lines.extend(
                    f"    • {prescription_line(item, catalog, lang)}" for item in block.items
                )
            else:
                lines.append(f"• {prescription_line(block.items[0], catalog, lang)}")
    if unmatched:
        lines.extend(["", t("plan.unmatched_title", lang)])
        lines.extend(f"• {name}" for name in unmatched)
    lines.append("")
    lines.append(t("disclosure.ai", lang))
    return "\n".join(lines)


def displayable_unmatched(unmatched: Sequence[str]) -> list[str]:
    """M8b: the "Not matched" names are model-authored display text (the user's own wording,
    condensed by the import agent), so they pass the AGENTS.md §3 wording check like a note
    does: an entry with a forbidden term is dropped, the rest are shown as they are."""
    return [name for name in unmatched if wording.first_forbidden_term(name) is None]


def split_message(text: str, *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split `text` into chunks of at most `limit` characters, on line boundaries where
    possible (a single over-long line is cut hard). Never returns an empty list."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


# --- Keyboards ------------------------------------------------------------------------------


def _new_plan_button(lang: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=t("plan.new_button", lang), callback_data=PlanMenu(action="new", plan_id=0).pack()
    )


def _paste_plan_button(lang: str) -> InlineKeyboardButton:
    """M8b "Paste my plan": the second entry point next to "New plan"."""
    return InlineKeyboardButton(
        text=t("plan.paste_button", lang), callback_data=PlanMenu(action="paste", plan_id=0).pack()
    )


def new_plan_markup(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_new_plan_button(lang)], [_paste_plan_button(lang)]]
    )


def plan_list_text(plans: Sequence[PlanRecord], lang: str) -> str:
    """The plan list (A§6.2): every plan by name, the default marked; no status — all plans
    are equal (A§4.3)."""
    lines = [t("plan.list_title", lang), ""]
    for record in plans:
        mark = f" — {_DEFAULT_MARK} {t('plan.default_marker', lang)}" if record.is_default else ""
        lines.append(f"• {record.name}{mark}")
    lines.extend(["", t("plan.list_hint", lang)])
    return "\n".join(lines)


def plan_list_markup(plans: Sequence[PlanRecord], lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for record in plans:
        label = f"{_DEFAULT_MARK} {record.name}" if record.is_default else record.name
        builder.add(
            InlineKeyboardButton(
                text=label, callback_data=PlanMenu(action="view", plan_id=record.id).pack()
            )
        )
    builder.add(_new_plan_button(lang), _paste_plan_button(lang))
    builder.adjust(1)
    return builder.as_markup()


def plan_actions_markup(record: PlanRecord, lang: str) -> InlineKeyboardMarkup:
    """Set default / Rename / Revise (A§6.2) for one plan, plus New plan, Paste and Back.
    Every plan offers every action (A§4.3: all plans are equal); only the default plan
    lacks "Set default", since it already is."""
    builder = InlineKeyboardBuilder()
    if not record.is_default:
        builder.add(
            InlineKeyboardButton(
                text=t("plan.set_default_button", lang),
                callback_data=PlanMenu(action="default", plan_id=record.id).pack(),
            )
        )
    builder.add(
        InlineKeyboardButton(
            text=t("plan.rename_button", lang),
            callback_data=PlanMenu(action="rename", plan_id=record.id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.revise_button", lang),
            callback_data=PlanMenu(action="revise", plan_id=record.id).pack(),
        ),
    )
    builder.add(
        _new_plan_button(lang),
        _paste_plan_button(lang),
        InlineKeyboardButton(
            text=t("plan.back_to_list_button", lang),
            callback_data=PlanMenu(action="list", plan_id=0).pack(),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def draft_markup(decision_id: int, lang: str) -> InlineKeyboardMarkup:
    """Confirm / Change something / Cancel (A§6.4 step 5)."""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("plan.confirm_button", lang),
            callback_data=PlanDraft(action="confirm", decision_id=decision_id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.change_button", lang),
            callback_data=PlanDraft(action="change", decision_id=decision_id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.cancel_button", lang),
            callback_data=PlanDraft(action="cancel", decision_id=decision_id).pack(),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def cancel_revision_markup(decision_id: int, lang: str) -> InlineKeyboardMarkup:
    """The single Cancel button under the "what should change?" prompt."""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("plan.cancel_button", lang),
            callback_data=PlanDraft(action="cancel", decision_id=decision_id).pack(),
        )
    )
    return builder.as_markup()
