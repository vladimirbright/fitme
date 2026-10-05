"""Free-text assistant in the bot (ADR 0003, ADR 0004): a message that no command, button
prompt or setup step claimed goes to `services.assistant.handle_message`; what it did is
shown here.

Called only from `bot/handlers/free_text.py`, after the message was saved and passed the
stop-word scan (A§6.3) — a halting message never reaches this module. The assistant's own
words come first (wording-checked in the service, marked as AI-generated), then every effect
in order, each built from the data, never from model text: a draft as a diff with Save / the
whole draft / Close, today's changed blocks, corrected sets with Undo, and the existing
screens for redesigns, new plans, block logging, stats.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from fitme.bot import plan_rendering
from fitme.bot.callback_data import AssistantUndo
from fitme.bot.handlers import plan as plan_handlers
from fitme.bot.handlers import stats as stats_handlers
from fitme.bot.handlers import train as train_handlers
from fitme.bot.rendering import refusal_text
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.catalog import Catalog
from fitme.domain.models import Plan, Workout
from fitme.i18n import t
from fitme.services import assistant, planning
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


def _as_plan(workout: Workout) -> Plan:
    return Plan(name=workout.title, schedule=[], workouts=[workout])


async def _show_today(message: Message, item: assistant.TodayChanged, lang: str) -> None:
    result = item.result
    if result.workout is None:
        lines = [t("assistant.today_not_changed", lang), *(f"• {e}" for e in result.errors)]
        await message.answer("\n".join(lines))
        return
    catalog = load_catalog()
    lines = [t("assistant.today_changed", lang)]
    lines.extend(
        plan_rendering.plan_diff_lines(
            _as_plan(item.before), _as_plan(result.workout), catalog, lang
        )
    )
    if result.requested is not None:
        lines.extend(
            plan_rendering.load_change_notes(
                _as_plan(result.requested), _as_plan(result.workout), catalog, lang
            )
        )
    await message.answer("\n".join(lines))


async def _show_saved(
    message: Message, db: Database, user_id: int, confirm: planning.ConfirmResult, lang: str
) -> None:
    status = confirm.status
    if status == planning.ConfirmStatus.SAVED:
        assert confirm.plan_id is not None and confirm.version is not None
        detail = await planning.get_plan_detail(db, user_id, confirm.plan_id)
        name = "" if detail is None else detail.record.name
        text = t("plan.saved", lang, name=name, version=confirm.version)
        if confirm.is_default:
            text = f"{text} {t('plan.saved_default', lang)}"
        await message.answer(text)
    elif status == planning.ConfirmStatus.ALREADY_SAVED:
        await message.answer(t("plan.already_saved", lang))
    elif status == planning.ConfirmStatus.REFUSED and confirm.refusal is not None:
        await message.answer(refusal_text(confirm.refusal, lang))
    else:
        await message.answer(t("plan.stale_draft", lang))


async def _show_item(
    message: Message,
    item: assistant.Item,
    *,
    db: Database,
    settings: Settings,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    lang: str,
    pending_plan_revisions: plan_handlers.PendingRevisions,
    pending_train: train_handlers.PendingTrains,
) -> None:
    if isinstance(item, assistant.DraftUpdated):
        await plan_handlers.show_edit_draft(message, item.result, item.before, lang)
    elif isinstance(item, assistant.TodayChanged):
        await _show_today(message, item, lang)
    elif isinstance(item, assistant.LogFixed):
        catalog = load_catalog()
        lines = [t("assistant.log_fixed_title", lang)]
        lines.extend(log_change_line(change, catalog, lang) for change in item.changes)
        await message.answer(
            "\n".join(lines), reply_markup=_undo_markup(item.undo_decision_id, lang)
        )
    elif isinstance(item, assistant.Notice):
        lines = [t(item.key, lang, name=item.name), *(f"• {d}" for d in item.details)]
        await message.answer("\n".join(lines))
    elif isinstance(item, assistant.DraftSaved):
        await _show_saved(message, db, user_id, item.confirm, lang)
    elif isinstance(item, assistant.RunRewrite):
        await plan_handlers.revise_from_base(message, db, llm, user_id, item.base, item.request)
    elif isinstance(item, assistant.RunNewPlan):
        await plan_handlers.new_plan(
            message, db, llm, user_id, lang, pending_plan_revisions, guidance=item.guidance
        )
    elif isinstance(item, assistant.RunLogBlock):
        await train_handlers.log_current_block(
            message, db, llm, user_id, pending_train, skip=item.skip
        )
    elif isinstance(item, assistant.RunEnterResults):
        # The user's own words go to the result parser, never the model's paraphrase.
        await train_handlers.enter_current_block_results(
            message, db, llm, user_id, text, pending_train
        )
    elif isinstance(item, assistant.RunShow):
        await _show_view(
            message, item, db=db, settings=settings, llm=llm, user_id=user_id, lang=lang
        )


async def _show_view(
    message: Message,
    item: assistant.RunShow,
    *,
    db: Database,
    settings: Settings,
    llm: LlmRuntime,
    user_id: int,
    lang: str,
) -> None:
    if item.view == "plan" and item.plan_id is not None:
        await plan_handlers.show_plan(message, db, user_id, item.plan_id, lang)
    elif item.view == "draft" and item.draft_decision_id is not None:
        await plan_handlers.show_draft(message, db, user_id, item.draft_decision_id, lang)
    elif item.view == "train":
        await train_handlers.cmd_train(message, db, settings, llm, user_id)
    elif item.view == "stats":
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
    pending_train: train_handlers.PendingTrains,
    *,
    chat_message_id: int | None = None,
) -> None:
    """The free-text handler's last step (A§6.3, ADR 0003/0004): after the stop-word scan,
    with no pending prompt. Always answers something."""
    lang = (await profile_service.get_snapshot(db, user_id)).language
    if message.bot is not None:
        await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    result = await assistant.handle_message(db, llm, user_id, text, chat_message_id=chat_message_id)
    if result.refusal is not None:
        await message.answer(refusal_text(result.refusal, lang))
        return
    if result.message:
        await message.answer(f"{result.message}\n\n{t('assistant.reply_footer', lang)}")
    for item in result.items:
        await _show_item(
            message,
            item,
            db=db,
            settings=settings,
            llm=llm,
            user_id=user_id,
            text=text,
            lang=lang,
            pending_plan_revisions=pending_plan_revisions,
            pending_train=pending_train,
        )
    if not result.message and not result.items:
        await message.answer(t("assistant.not_understood", lang))


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
