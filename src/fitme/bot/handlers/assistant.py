"""Free-text assistant in the bot (ADR 0003): a message that no command, button prompt or
setup step claimed goes to `services.assistant.handle_message`, and its outcome is shown here.

Called only from `bot/handlers/free_text.py`, after the message was saved and passed the
stop-word scan (A§6.3) — a halting message never reaches this module. Edits are applied
immediately; every applied edit is shown as a deterministic diff (never model text) with an
**Undo** button. A model reply is shown as-is (it passed the wording check in the service),
marked as AI-generated.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from fitme.bot import plan_rendering
from fitme.bot.callback_data import AssistantUndo
from fitme.bot.handlers import plan as plan_handlers
from fitme.bot.handlers import stats as stats_handlers
from fitme.bot.handlers import train as train_handlers
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.catalog import Catalog
from fitme.domain.models import Plan, Prescription
from fitme.i18n import t
from fitme.services import assistant
from fitme.services import profile as profile_service
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.log_edit import SetChange


def _undo_markup(decision_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("assistant.undo_button", lang),
                    callback_data=AssistantUndo(decision_id=decision_id).pack(),
                )
            ]
        ]
    )


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
        line = plan_rendering.prescription_line(item, catalog, lang)
        if key not in old:
            lines.append(t("assistant.line_added", lang, workout=key[0], line=line))
        elif old[key] != item:
            lines.append(t("assistant.line_changed", lang, workout=key[0], line=line))
    for key, item in old.items():
        if key not in new:
            name = plan_rendering.exercise_name(
                catalog.by_id(item.exercise_id), item.exercise_id, lang
            )
            lines.append(t("assistant.line_removed", lang, workout=key[0], name=name))
    return lines


def _set_value(reps: int | None, kg: float | None, lang: str) -> str:
    if reps is None:
        return t("assistant.log_not_logged", lang)
    if kg is None:
        return t("assistant.log_value_bodyweight", lang, reps=reps)
    return t("assistant.log_value", lang, reps=reps, kg=t("plan.load_kg", lang, kg=f"{kg:g}"))


def log_change_line(change: SetChange, catalog: Catalog, lang: str) -> str:
    name = plan_rendering.exercise_name(catalog.by_id(change.exercise_id), change.exercise_id, lang)
    return t(
        "assistant.log_line",
        lang,
        name=name,
        number=change.set_number,
        after=_set_value(change.after_reps, change.after_kg, lang),
        before=_set_value(change.before_reps, change.before_kg, lang),
    )


async def _show_plan_edited(message: Message, result: assistant.PlanEdited, lang: str) -> None:
    lines: list[str] = []
    if result.before is not None and result.after is not None:
        lines.append(t("assistant.saved_title", lang, name=result.plan_name))
        lines.extend(plan_diff_lines(result.before, result.after, load_catalog(), lang))
    if result.renamed_to is not None:
        lines.append(t("assistant.renamed", lang, name=result.renamed_to))
    if result.made_default:
        lines.append(t("assistant.made_default", lang, name=result.plan_name))
    if result.rename_failed is not None:
        lines.append(t(result.rename_failed, lang))
    markup = (
        None if result.undo_decision_id is None else _undo_markup(result.undo_decision_id, lang)
    )
    await message.answer("\n".join(lines), reply_markup=markup)


async def _open_flow(
    message: Message,
    db: Database,
    settings: Settings,
    llm: LlmRuntime,
    user_id: int,
    flow: assistant.OpenFlow,
    lang: str,
    pending_plan_revisions: plan_handlers.PendingRevisions,
) -> None:
    if flow.action == "show_plan" and flow.plan_id is not None:
        await plan_handlers.show_plan(message, db, user_id, flow.plan_id, lang)
    elif flow.action == "new_plan":
        await plan_handlers.new_plan(
            message, db, llm, user_id, lang, pending_plan_revisions, guidance=flow.request
        )
    elif flow.action == "revise_plan" and flow.plan_id is not None and flow.request:
        await plan_handlers.revise_plan_from_text(
            message, db, llm, user_id, flow.plan_id, flow.request
        )
    elif flow.action == "train":
        await train_handlers.cmd_train(message, db, settings, llm, user_id)
    elif flow.action == "stats":
        await stats_handlers.cmd_stats(message, db, settings, user_id)
    else:
        await plan_handlers.show_plan_list(message, db, user_id, lang)


async def handle_assistant_text(
    message: Message,
    db: Database,
    settings: Settings,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    pending_plan_revisions: plan_handlers.PendingRevisions,
) -> None:
    """The free-text handler's last step (A§6.3, ADR 0003): after the stop-word scan, with
    no pending prompt. Always answers something."""
    lang = (await profile_service.get_snapshot(db, user_id)).language
    if message.bot is not None:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    outcome = await assistant.handle_message(db, llm, user_id, text)

    if isinstance(outcome, assistant.PlanEdited):
        await _show_plan_edited(message, outcome, lang)
    elif isinstance(outcome, assistant.LogFixed):
        catalog = load_catalog()
        lines = [t("assistant.log_fixed_title", lang)]
        lines.extend(log_change_line(change, catalog, lang) for change in outcome.changes)
        await message.answer(
            "\n".join(lines), reply_markup=_undo_markup(outcome.undo_decision_id, lang)
        )
    elif isinstance(outcome, assistant.OpenFlow):
        await _open_flow(message, db, settings, llm, user_id, outcome, lang, pending_plan_revisions)
    elif isinstance(outcome, assistant.Replied):
        await message.answer(f"{outcome.text}\n\n{t('assistant.reply_footer', lang)}")
    elif isinstance(outcome, assistant.Refused):
        # Per-code copy, never model text (as `/plan` shows refusals).
        await message.answer(t(f"refusal.{outcome.refusal.code.value}", lang))
    else:
        lines = [t(outcome.key, lang)]
        lines.extend(f"• {detail}" for detail in outcome.details)
        await message.answer("\n".join(lines))


async def on_undo(
    query: CallbackQuery,
    callback_data: AssistantUndo,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
) -> None:
    lang = (await profile_service.get_snapshot(db, user_id)).language
    result = await assistant.undo(db, llm, user_id, callback_data.decision_id)
    if result.ok:
        await query.answer(t(result.key, lang))
        message = query.message
        if isinstance(message, Message):
            await message.answer(t(result.key, lang))
        return
    if not result.details:
        await query.answer(t(result.key, lang), show_alert=True)
        return
    await query.answer()
    message = query.message
    if isinstance(message, Message):
        lines = [t(result.key, lang), *(f"• {detail}" for detail in result.details)]
        await message.answer("\n".join(lines))


def build_router() -> Router:
    router = Router(name="assistant")
    router.callback_query.register(on_undo, AssistantUndo.filter())
    return router
