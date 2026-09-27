"""`/plan` (A§6.2, A§6.4): list plans with actions, generate a draft, show it with Confirm /
Change something / Cancel, loop through revisions, and confirm.

Everything that decides anything lives in `services.planning` (the bot and, in M9, the web
share it). This module only routes buttons and text, renders through `bot.plan_rendering`
and i18n, and keeps one piece of transient UI state: `pending_plan_revisions` (per user,
which draft or plan a "what should change?" prompt is waiting on). It's in-memory dispatcher
data like `pending_deletes` — a restart just forgets the prompt, and the user taps again.

Free text for a pending revision arrives through `bot/handlers/free_text.py`, which saves it
and runs the stop-word guard first (A§6.3); only then does `handle_revise_text` here call the
service. A stop-word hit halts and drops the pending revision.

Stale buttons (A§6.3): Confirm/Change/Cancel carry the draft's decision id, which must be the
user's *current* draft (`services.planning.current_draft_id`); anything else gets a toast.
`confirm_plan` re-checks this inside its own transaction, so the toast is a courtesy, not
the guard.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from fitme.bot import plan_rendering as rendering
from fitme.bot.callback_data import PlanDraft, PlanMenu
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.models import Plan
from fitme.i18n import t
from fitme.services import planning
from fitme.services import profile as profile_service
from fitme.services.llm_runtime import LlmRuntime


@dataclass(frozen=True, slots=True)
class PendingRevision:
    base: planning.RevisionBase


PendingRevisions = dict[int, PendingRevision]


async def _lang(db: Database, user_id: int) -> str:
    return (await profile_service.get_snapshot(db, user_id)).language


def _message_of(query: CallbackQuery) -> Message:
    message = query.message
    assert isinstance(message, Message)  # a stranger can't reach a bot keyboard (A§6.1)
    return message


async def _send_plan(
    message: Message, plan: Plan, *, title: str, lang: str, markup: InlineKeyboardMarkup
) -> None:
    text = rendering.render_plan_text(plan, catalog=load_catalog(), lang=lang, title=title)
    chunks = rendering.split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=markup)


async def _show_round(message: Message, result: planning.PlanRoundResult, lang: str) -> None:
    refusal = result.refusal
    if refusal is not None:
        # AGENTS.md §2: a refusal is a valid output. Shown via the per-code i18n copy, never
        # model text; `llm_unavailable`'s copy is the "try again later" message.
        await message.answer(t(f"refusal.{refusal.code.value}", lang))
        return
    plan = result.plan
    assert plan is not None
    await _send_plan(
        message,
        plan,
        title=t("plan.draft_title", lang),
        lang=lang,
        markup=rendering.draft_markup(result.decision_id, lang),
    )


async def _show_list(message: Message, db: Database, user_id: int, lang: str) -> None:
    plans = await planning.list_plans(db, user_id)
    if not plans:
        await message.answer(t("plan.none_yet", lang), reply_markup=rendering.new_plan_markup(lang))
        return
    await message.answer(
        rendering.plan_list_text(plans, lang), reply_markup=rendering.plan_list_markup(plans, lang)
    )


async def _generate(
    message: Message, db: Database, llm: LlmRuntime, user_id: int, lang: str
) -> None:
    # A short "generating" message: the model call takes a while, and the DB lock is never
    # held across it (A§4.6), so other updates keep flowing meanwhile.
    await message.answer(t("plan.generating", lang))
    result = await planning.propose_new_plan(db, llm, user_id)
    await _show_round(message, result, lang)


async def cmd_plan(message: Message, db: Database, user_id: int) -> None:
    lang = await _lang(db, user_id)
    await _show_list(message, db, user_id, lang)


async def on_plan_menu(
    query: CallbackQuery,
    callback_data: PlanMenu,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    pending_plan_revisions: PendingRevisions,
) -> None:
    lang = await _lang(db, user_id)
    message = _message_of(query)
    action = callback_data.action

    if action == "new":
        await query.answer()
        await _generate(message, db, llm, user_id, lang)
        return
    if action == "list":
        await query.answer()
        await _show_list(message, db, user_id, lang)
        return

    detail = await planning.get_plan_detail(db, user_id, callback_data.plan_id)
    if detail is None:
        await query.answer(t("errors.stale_callback", lang))
        return

    if action == "view":
        await query.answer()
        default = t("plan.default_suffix", lang) if detail.record.is_default else ""
        title = t(
            "plan.plan_title",
            lang,
            name=detail.record.name,
            version=detail.version.version,
            status=rendering.plan_status_text(detail.record, lang),
            default=default,
        )
        await _send_plan(
            message,
            detail.plan,
            title=title,
            lang=lang,
            markup=rendering.plan_actions_markup(detail.record, lang),
        )
        return
    if action == "default":
        if await planning.set_default(db, user_id, detail.record.id):
            await query.answer(t("plan.default_set", lang, name=detail.record.name))
        else:
            await query.answer(t("plan.default_not_set", lang), show_alert=True)
        await _show_list(message, db, user_id, lang)
        return
    if action == "archive":
        if await planning.archive(db, user_id, detail.record.id):
            await query.answer(t("plan.archived", lang, name=detail.record.name))
        else:
            await query.answer(t("plan.archive_failed", lang), show_alert=True)
        await _show_list(message, db, user_id, lang)
        return
    if action == "revise":
        await query.answer()
        pending_plan_revisions[user_id] = PendingRevision(
            base=planning.PlanBase(plan_id=detail.record.id)
        )
        await message.answer(t("plan.revise_of", lang, name=detail.record.name))
        await message.answer(
            t("plan.change_prompt", lang), reply_markup=rendering.cancel_revision_markup(0, lang)
        )
        return
    await query.answer(t("errors.stale_callback", lang))


async def on_plan_draft(
    query: CallbackQuery,
    callback_data: PlanDraft,
    db: Database,
    settings: Settings,
    user_id: int,
    pending_plan_revisions: PendingRevisions,
) -> None:
    lang = await _lang(db, user_id)
    message = _message_of(query)
    action = callback_data.action
    decision_id = callback_data.decision_id

    if action == "cancel":
        await query.answer()
        had_pending = pending_plan_revisions.pop(user_id, None) is not None
        current = await planning.current_draft_id(db, user_id)
        if had_pending or (decision_id and current == decision_id):
            await message.answer(t("plan.change_cancelled", lang))
        else:
            await message.answer(t("plan.nothing_to_cancel", lang))
        return

    if action == "confirm":
        # No staleness pre-check here: `confirm_plan` decides in its own transaction, and it
        # answers a double-tap with ALREADY_SAVED before the (now stale) draft check.
        pending_plan_revisions.pop(user_id, None)
        result = await planning.confirm_plan(db, settings, user_id, decision_id)
        await _show_confirm(query, message, db, user_id, result, lang)
        return

    if await planning.current_draft_id(db, user_id) != decision_id:
        await query.answer(t("plan.stale_draft", lang), show_alert=True)
        return

    if action == "change":
        await query.answer()
        pending_plan_revisions[user_id] = PendingRevision(
            base=planning.DraftBase(decision_id=decision_id)
        )
        await message.answer(
            t("plan.change_prompt", lang),
            reply_markup=rendering.cancel_revision_markup(decision_id, lang),
        )
        return

    await query.answer(t("errors.stale_callback", lang))


async def _show_confirm(
    query: CallbackQuery,
    message: Message,
    db: Database,
    user_id: int,
    result: planning.ConfirmResult,
    lang: str,
) -> None:
    status = result.status
    if status == planning.ConfirmStatus.SAVED:
        await query.answer()
        assert result.plan_id is not None and result.version is not None
        detail = await planning.get_plan_detail(db, user_id, result.plan_id)
        name = "" if detail is None else detail.record.name
        text = t("plan.saved", lang, name=name, version=result.version)
        if result.is_default:
            text = f"{text} {t('plan.saved_default', lang)}"
        await message.answer(text)
    elif status == planning.ConfirmStatus.ALREADY_SAVED:
        await query.answer(t("plan.already_saved", lang))
    elif status == planning.ConfirmStatus.REFUSED:
        await query.answer()
        assert result.refusal is not None
        await message.answer(t(f"refusal.{result.refusal.code.value}", lang))
    else:  # STALE, NOT_FOUND
        await query.answer(t("plan.stale_draft", lang), show_alert=True)


async def handle_revise_text(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    pending_plan_revisions: PendingRevisions,
) -> bool:
    """Called by the free-text handler *after* the stop-word scan (A§6.3). Returns `True`
    if a revision was pending and the text was consumed by it."""
    pending = pending_plan_revisions.pop(user_id, None)
    if pending is None:
        return False
    lang = await _lang(db, user_id)
    await message.answer(t("plan.revising", lang))
    try:
        result = await planning.revise_plan(db, llm, user_id, pending.base, text)
    except planning.StaleDraftError:
        await message.answer(t("plan.stale_draft", lang))
        return True
    except planning.PlanNotFoundError:
        await message.answer(t("plan.not_found", lang))
        return True
    await _show_round(message, result, lang)
    return True


def build_router() -> Router:
    router = Router(name="plan")
    router.message.register(cmd_plan, Command("plan"))
    router.callback_query.register(on_plan_menu, PlanMenu.filter())
    router.callback_query.register(on_plan_draft, PlanDraft.filter())
    return router
