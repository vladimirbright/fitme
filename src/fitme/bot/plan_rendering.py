"""Pure text/keyboard builders for `/plan` (A§6.4 step 5): a plan as structured text —
schedule with localized weekday names, then each workout with its exercises, sets × reps,
rest, and an **explicit load** on every prescription — plus the plan-list and draft
keyboards, and the Telegram message-length split.

Nothing here touches the database or the LLM: callers pass the loaded `Plan`, the catalog
(for localized exercise names and each exercise's stimulus) and the language.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fitme.bot.callback_data import PlanDraft, PlanMenu, TrainPick
from fitme.db.records import PlanRecord
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import BlockRole, Measure
from fitme.domain.models import Block, Plan, Prescription
from fitme.i18n import t, wording
from fitme.services import prescribing, session_time
from fitme.services.prescribing import load_label

TELEGRAM_MESSAGE_LIMIT = 4096
_DEFAULT_MARK = "★"


def exercise_name(exercise: Exercise | None, exercise_id: str, lang: str) -> str:
    if exercise is None:
        return exercise_id
    return exercise.names.get(lang) or exercise.names.get("en") or exercise_id


def rest_text(seconds: int, lang: str) -> str:
    """Rest as people say it: "45 s", "2 min", "2 min 30 s"."""
    minutes, rest = divmod(seconds, 60)
    if minutes == 0:
        return t("duration.seconds", lang, s=rest)
    if rest == 0:
        return t("duration.minutes", lang, m=minutes)
    return t("duration.minutes_seconds", lang, m=minutes, s=rest)


def reps_text(reps_min: int, reps_max: int) -> str:
    """ "8" for a fixed count, "6–8" for a range."""
    return str(reps_min) if reps_min == reps_max else f"{reps_min}–{reps_max}"


def target_text(prescription: Prescription, lang: str) -> str:
    """One set's target in its own measure (ADR 0009): "6–8" (reps), "30 s" / "2 min"
    (time), "40 m" (distance)."""
    if prescription.measure == Measure.TIME:
        assert prescription.seconds is not None  # Prescription's validator guarantees this
        return rest_text(prescription.seconds, lang)
    if prescription.measure == Measure.DISTANCE:
        assert prescription.meters is not None
        return t("duration.meters", lang, m=prescription.meters)
    assert prescription.reps_min is not None and prescription.reps_max is not None
    return reps_text(prescription.reps_min, prescription.reps_max)


def prescription_load_label(
    prescription: Prescription,
    exercise: Exercise | None,
    lang: str,
    *,
    role: BlockRole | None = None,
) -> str:
    """The plan view's load text (ADR 0011, `services.prescribing`): the number, or how to
    pick an unknown load for this exercise's stimulus, never a catalog start weight."""
    return prescribing.prescription_load_label(prescription, exercise, lang, role=role)


def prescription_line(
    prescription: Prescription,
    catalog: Catalog,
    lang: str,
    *,
    in_superset: str | None = None,
    role: BlockRole | None = None,
) -> str:
    """One exercise as a line. A single block says its rest. ADR 0011: inside a superset
    (`in_superset="transition"`) the line says the transition to the next exercise instead,
    and the last exercise (`"last"`) says no rest at all — the round's rest is said once, in
    the superset's heading."""
    exercise = catalog.by_id(prescription.exercise_id)
    target = target_text(prescription, lang)
    if exercise is not None and exercise.unilateral:
        target = t("plan.per_side", lang, target=target)  # ADR 0016
    sets_reps = t("plan.sets_reps", lang, sets=prescription.sets, reps=target)
    name = exercise_name(exercise, prescription.exercise_id, lang)
    load = prescription_load_label(prescription, exercise, lang, role=role)
    if in_superset is None:
        rest = rest_text(prescription.rest_seconds, lang)
        line = t(
            "plan.prescription_line", lang, name=name, sets_reps=sets_reps, load=load, rest=rest
        )
    else:
        line = t("plan.superset_item_line", lang, name=name, sets_reps=sets_reps, load=load)
        if in_superset == "transition":
            line = f"{line}, {transition_text(prescription.rest_seconds, lang)}"
    if prescription.note:
        line = f"{line} — {prescription.note}"
    return line


def transition_text(seconds: int, lang: str) -> str:
    """ "then 20 s to the next exercise" / "then straight to the next exercise"."""
    if seconds <= 0:
        return t("plan.superset_no_transition", lang)
    return t("plan.superset_transition", lang, rest=rest_text(seconds, lang))


def block_lines(block: Block, catalog: Catalog, lang: str) -> list[str]:
    """A block's lines: a single exercise, or a superset's heading (rounds, the one rest
    after each round) and its exercises in order (ADR 0009, ADR 0011)."""
    if block.kind != "superset":
        return [f"• {prescription_line(block.items[0], catalog, lang, role=block.role)}"]
    lines = [
        t(
            "plan.superset_rounds_label",
            lang,
            rounds=max(item.sets for item in block.items),
            rest=rest_text(block.items[-1].rest_seconds, lang),
        )
    ]
    last = len(block.items) - 1
    for index, item in enumerate(block.items):
        position = "last" if index == last else "transition"
        line = prescription_line(item, catalog, lang, in_superset=position, role=block.role)
        lines.append(f"    • {line}")
    return lines


def workout_block_lines(blocks: Sequence[Block], catalog: Catalog, lang: str) -> list[str]:
    """Every block of a workout, with a role heading (ADR 0011) whenever the role changes."""
    lines: list[str] = []
    previous: BlockRole | None = None
    for block in blocks:
        if block.role is not None and block.role != previous:
            role = t(f"enum.block_role.{block.role.value}", lang)
            lines.append(t("plan.block_role_heading", lang, role=role))
        previous = block.role
        lines.extend(block_lines(block, catalog, lang))
    return lines


def _keyed(plan: Plan) -> dict[tuple[str, str, int], Prescription]:
    """Every prescription keyed by (workout key, exercise id, occurrence in that workout)."""
    keyed: dict[tuple[str, str, int], Prescription] = {}
    for workout in plan.workouts:
        seen: dict[str, int] = {}
        for block in workout.blocks:
            for item in block.items:
                seen[item.exercise_id] = seen.get(item.exercise_id, 0) + 1
                keyed[(workout.key, item.exercise_id, seen[item.exercise_id])] = item
    return keyed


def plan_diff_lines(before: Plan, after: Plan, catalog: Catalog, lang: str) -> list[str]:
    """What changed between two versions of a plan, one line per change, built from the
    plans themselves (no model text)."""
    lines: list[str] = []
    if before.schedule != after.schedule:
        days = ", ".join(
            t(
                "assistant.schedule_day",
                lang,
                weekday=t(f"weekday.{day.weekday}", lang),
                workout=day.workout_key,
            )
            for day in after.schedule
        )
        lines.append(t("assistant.schedule_changed", lang, days=days))
    titles_before = {w.key: w.title for w in before.workouts}
    for workout in after.workouts:
        if titles_before.get(workout.key, workout.title) != workout.title:
            lines.append(
                t("assistant.title_changed", lang, workout=workout.key, title=workout.title)
            )
    old, new = _keyed(before), _keyed(after)
    for key, item in new.items():
        line = prescription_line(item, catalog, lang)
        if key not in old:
            lines.append(t("assistant.line_added", lang, workout=key[0], line=line))
        elif old[key] != item:
            lines.append(t("assistant.line_changed", lang, workout=key[0], line=line))
    for key, item in old.items():
        if key not in new:
            name = exercise_name(catalog.by_id(item.exercise_id), item.exercise_id, lang)
            lines.append(t("assistant.line_removed", lang, workout=key[0], name=name))
    return lines


def load_change_notes(requested: Plan, judged: Plan, catalog: Catalog, lang: str) -> list[str]:
    """One line per load the guards replaced (ADR 0004): what was asked, what it is now."""
    notes: list[str] = []
    for asked_workout, judged_workout in zip(requested.workouts, judged.workouts, strict=False):
        asked_items = [i for b in asked_workout.blocks for i in b.items]
        judged_items = [i for b in judged_workout.blocks for i in b.items]
        for asked, got in zip(asked_items, judged_items, strict=False):
            if asked.exercise_id != got.exercise_id or asked.load == got.load:
                continue
            exercise = catalog.by_id(asked.exercise_id)
            notes.append(
                t(
                    "assistant.load_limited",
                    lang,
                    name=exercise_name(exercise, asked.exercise_id, lang),
                    asked=load_label(asked.load, exercise, lang),
                    got=prescription_load_label(got, exercise, lang),
                )
            )
    return notes


def session_draft_markup(decision_id: int, lang: str) -> InlineKeyboardMarkup:
    """Under a planning session's draft (ADR 0004): Save / the whole draft / Close without
    saving. Changes are typed as messages, so there is no "Change" button."""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("plan.save_button", lang),
            callback_data=PlanDraft(action="confirm", decision_id=decision_id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.view_draft_button", lang),
            callback_data=PlanDraft(action="view", decision_id=decision_id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.close_session_button", lang),
            callback_data=PlanDraft(action="cancel", decision_id=decision_id).pack(),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


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
        if workout.qualities:
            names = ", ".join(t(f"enum.quality.{q.value}", lang) for q in workout.qualities)
            lines.append(t("plan.workout_qualities", lang, qualities=names))
        if workout.blocks:
            minutes = session_time.display_minutes(workout, catalog)
            lines.append(t("plan.workout_estimate", lang, minutes=minutes))
        lines.extend(workout_block_lines(workout.blocks, catalog, lang))
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
    """Set default / Rename / Revise (A§6.2) for one plan, plus New plan, Paste, Back and,
    last, Start training (the `/train` suggestion for this plan). Every plan offers every
    action (A§4.3: all plans are equal); only the default plan lacks "Set default", since it
    already is."""
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
        InlineKeyboardButton(
            text=t("plan.delete_button", lang),
            callback_data=PlanMenu(action="delete", plan_id=record.id).pack(),
        )
    )
    builder.add(
        _new_plan_button(lang),
        _paste_plan_button(lang),
        InlineKeyboardButton(
            text=t("plan.back_to_list_button", lang),
            callback_data=PlanMenu(action="list", plan_id=0).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.start_training_button", lang),
            callback_data=TrainPick(kind="plan", plan_id=record.id).pack(),
        ),
    )
    builder.adjust(1)
    return builder.as_markup()


def delete_confirm_markup(plan_id: int, lang: str) -> InlineKeyboardMarkup:
    """Yes / No for the delete confirm step (A§4.3), stale-safe like every other `/plan`
    callback: both buttons carry the plan id, which `on_plan_menu` re-checks against the
    user's own plans before deleting anything."""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(
            text=t("plan.delete_yes_button", lang),
            callback_data=PlanMenu(action="delete_confirm", plan_id=plan_id).pack(),
        ),
        InlineKeyboardButton(
            text=t("plan.delete_no_button", lang),
            callback_data=PlanMenu(action="delete_cancel", plan_id=plan_id).pack(),
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


def new_plan_guidance_markup(lang: str) -> InlineKeyboardMarkup:
    """Under the "what should the new plan be?" question: generate without guidance, or
    Cancel (the same pending-prompt Cancel as a revision)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("plan.new_without_guidance_button", lang),
            callback_data=PlanMenu(action="new_skip", plan_id=0).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t("plan.cancel_button", lang),
            callback_data=PlanDraft(action="cancel", decision_id=0).pack(),
        )
    )
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
