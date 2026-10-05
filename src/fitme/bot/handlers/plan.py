"""`/plan` (A§6.2, A§6.4): list plans with actions (view, set default, rename, revise),
generate a draft — or paste an existing program (M8b "Paste my plan") — show it with
Confirm / Change something / Cancel, loop through revisions, and confirm. All plans are equal
(A§4.3): there is no archived state, and every plan offers every action.

Everything that decides anything lives in `services.planning` (the bot and, in M9, the web
share it). This module only routes buttons and text, renders through `bot.plan_rendering`
and i18n, and keeps one piece of transient UI state: `pending_plan_revisions` (per user,
which draft or plan a "what should change?" prompt is waiting on, that a "paste your
program" prompt is, or which plan a "new name?" prompt is for). It's in-memory dispatcher
data like `pending_deletes` — a restart just forgets the prompt, and the user taps again.

Free text for a pending revision, import or rename arrives through
`bot/handlers/free_text.py`, which saves it and runs the stop-word guard first (A§6.3); only
then does `handle_plan_text` here call the service. A stop-word hit halts and drops the
pending prompt.

Stale buttons (A§6.3): Confirm/Change/Cancel carry the draft's decision id, which must be the
user's *current* draft (`services.planning.current_draft_id`); anything else gets a toast.
`confirm_plan` re-checks this inside its own transaction, so the toast is a courtesy, not
the guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from fitme.bot import plan_rendering as rendering
from fitme.bot.callback_data import PlanDraft, PlanMenu
from fitme.bot.rendering import refusal_text
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.models import NAME_MAX_LENGTH, Plan
from fitme.i18n import t
from fitme.services import planning
from fitme.services import profile as profile_service
from fitme.services.llm_runtime import LlmRuntime


@dataclass(frozen=True, slots=True)
class PendingRevision:
    base: planning.RevisionBase


@dataclass(frozen=True, slots=True)
class PendingImport:
    """M8b: the next free text is the program to paste."""


@dataclass(frozen=True, slots=True)
class PendingNewPlan:
    """The next free text describes the new plan to generate: asked when the user already
    has plans, so the new one isn't a near-copy of them."""


@dataclass(frozen=True, slots=True)
class PendingRename:
    """The next free text is the new name for `plan_id` (`services.planning.rename_plan`)."""

    plan_id: int


PendingPlanText = PendingRevision | PendingImport | PendingRename | PendingNewPlan
PendingRevisions = dict[int, PendingPlanText]


async def _lang(db: Database, user_id: int) -> str:
    return (await profile_service.get_snapshot(db, user_id)).language


def _message_of(query: CallbackQuery) -> Message:
    message = query.message
    assert isinstance(message, Message)  # a stranger can't reach a bot keyboard (A§6.1)
    return message


async def _send_plan(
    message: Message,
    plan: Plan,
    *,
    title: str,
    lang: str,
    markup: InlineKeyboardMarkup,
    unmatched: Sequence[str] = (),
) -> None:
    text = rendering.render_plan_text(
        plan, catalog=load_catalog(), lang=lang, title=title, unmatched=unmatched
    )
    chunks = rendering.split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=markup)


async def _show_round(
    message: Message,
    result: planning.PlanRoundResult,
    lang: str,
    *,
    title_key: str = "plan.draft_title",
) -> None:
    refusal = result.refusal
    if refusal is not None:
        # AGENTS.md §2: a refusal is a valid output. Per-code copy (`llm_unavailable`'s is
        # "try again later"), or the model's own explanation for `out_of_scope`.
        await message.answer(refusal_text(refusal, lang))
        return
    plan = result.plan
    assert plan is not None
    await _send_plan(
        message,
        plan,
        title=t(title_key, lang),
        lang=lang,
        markup=rendering.draft_markup(result.decision_id, lang),
        unmatched=result.unmatched,
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
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    lang: str,
    guidance: str | None = None,
) -> None:
    # A short "generating" message: the model call takes a while, and the DB lock is never
    # held across it (A§4.6), so other updates keep flowing meanwhile.
    await message.answer(t("plan.generating", lang))
    result = await planning.propose_new_plan(db, llm, user_id, guidance=guidance)
    await _show_round(message, result, lang)


async def _ask_or_generate(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    lang: str,
    pending_plan_revisions: PendingRevisions,
) -> None:
    """ "New plan": with no plans yet, generate straight from the profile. With plans, first
    ask what the new one should be — the answer arrives as free text (stop-word scan first,
    `free_text.py`) and goes to the generator as guidance; "Generate anyway" skips it."""
    plans = await planning.list_plans(db, user_id)
    if not plans or await planning.is_gated(db, user_id):
        # Nothing to tell apart, or the gate refuses anyway: go straight to the round (which
        # logs the refusal as usual) instead of asking a question that leads nowhere.
        await _generate(message, db, llm, user_id, lang)
        return
    pending_plan_revisions[user_id] = PendingNewPlan()
    names = ", ".join(f"“{plan.name}”" for plan in plans)
    await message.answer(
        t("plan.new_guidance_prompt", lang, names=names),
        reply_markup=rendering.new_plan_guidance_markup(lang),
    )


async def _send_detail(message: Message, detail: planning.PlanDetail, lang: str) -> None:
    default = t("plan.default_suffix", lang) if detail.record.is_default else ""
    title = t(
        "plan.plan_title",
        lang,
        name=detail.record.name,
        version=detail.version.version,
        default=default,
    )
    await _send_plan(
        message,
        detail.plan,
        title=title,
        lang=lang,
        markup=rendering.plan_actions_markup(detail.record, lang),
    )


# Entry points for the free-text assistant (`bot/handlers/assistant.py`, ADR 0003): the same
# screens the buttons open, so a typed "show my plan" lands exactly where a tap would.


async def show_plan_list(message: Message, db: Database, user_id: int, lang: str) -> None:
    await _show_list(message, db, user_id, lang)


async def show_plan(message: Message, db: Database, user_id: int, plan_id: int, lang: str) -> None:
    detail = await planning.get_plan_detail(db, user_id, plan_id)
    if detail is None:
        await message.answer(t("plan.not_found", lang))
        return
    await _send_detail(message, detail, lang)


async def new_plan(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    lang: str,
    pending_plan_revisions: PendingRevisions,
    guidance: str | None = None,
) -> None:
    """A typed "new plan": the user's own description is the guidance when they gave one;
    otherwise the same as tapping New plan (ask first when plans exist)."""
    if guidance and guidance.strip():
        await _generate(message, db, llm, user_id, lang, guidance)
        return
    await _ask_or_generate(message, db, llm, user_id, lang, pending_plan_revisions)


async def revise_plan_from_text(
    message: Message, db: Database, llm: LlmRuntime, user_id: int, plan_id: int, text: str
) -> None:
    """A whole-plan rewrite stays a *draft* with Confirm/Change/Cancel (A§6.4): the model
    designs it, so the owner confirms it — unlike a direct, specific edit."""
    lang = await _lang(db, user_id)
    await message.answer(t("plan.revising", lang))
    try:
        result = await planning.revise_plan(db, llm, user_id, planning.PlanBase(plan_id), text)
    except planning.PlanNotFoundError:
        await message.answer(t("plan.not_found", lang))
        return
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
        await _ask_or_generate(message, db, llm, user_id, lang, pending_plan_revisions)
        return
    if action == "new_skip":
        if not isinstance(pending_plan_revisions.get(user_id), PendingNewPlan):
            await query.answer(t("errors.stale_callback", lang))
            return
        await query.answer()
        pending_plan_revisions.pop(user_id, None)
        await _generate(message, db, llm, user_id, lang)
        return
    if action == "paste":
        # M8b: the next free text is the program; it goes through the stop-word guard first
        # (`free_text.py`), then `handle_plan_text` -> `planning.import_plan`.
        await query.answer()
        pending_plan_revisions[user_id] = PendingImport()
        await message.answer(
            t("plan.paste_prompt", lang), reply_markup=rendering.cancel_revision_markup(0, lang)
        )
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
        await _send_detail(message, detail, lang)
        return
    if action == "default":
        if await planning.set_default(db, user_id, detail.record.id):
            await query.answer(t("plan.default_set", lang, name=detail.record.name))
        else:
            await query.answer(t("plan.default_not_set", lang), show_alert=True)
        await _show_list(message, db, user_id, lang)
        return
    if action == "rename":
        await query.answer()
        pending_plan_revisions[user_id] = PendingRename(plan_id=detail.record.id)
        await message.answer(
            t("plan.rename_prompt", lang, name=detail.record.name),
            reply_markup=rendering.cancel_revision_markup(0, lang),
        )
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
    if action == "delete":
        await query.answer()
        await message.answer(
            t("plan.delete_confirm_prompt", lang, name=detail.record.name),
            reply_markup=rendering.delete_confirm_markup(detail.record.id, lang),
        )
        return
    if action == "delete_confirm":
        await query.answer()
        result = await planning.delete_plan(db, user_id, detail.record.id)
        if result.status != planning.DeleteStatus.OK:
            await message.answer(t("plan.not_found", lang))
            return
        await message.answer(t("plan.deleted", lang, name=detail.record.name))
        await _show_list(message, db, user_id, lang)
        return
    if action == "delete_cancel":
        await query.answer()
        await message.answer(t("plan.delete_cancelled", lang))
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
        pending = pending_plan_revisions.pop(user_id, None)
        if isinstance(pending, PendingImport):
            await message.answer(t("plan.paste_cancelled", lang))
            return
        if isinstance(pending, PendingRename):
            await message.answer(t("plan.rename_cancelled", lang))
            return
        if isinstance(pending, PendingNewPlan):
            await message.answer(t("plan.new_cancelled", lang))
            return
        current = await planning.current_draft_id(db, user_id)
        if pending is not None or (decision_id and current == decision_id):
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
        await message.answer(refusal_text(result.refusal, lang))
    else:  # STALE, NOT_FOUND
        await query.answer(t("plan.stale_draft", lang), show_alert=True)


async def handle_plan_text(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    pending_plan_revisions: PendingRevisions,
) -> bool:
    """Called by the free-text handler *after* the stop-word scan (A§6.3). Returns `True`
    if a revision, an import (M8b), a rename or a new plan's guidance was pending and the
    text was consumed by it."""
    pending = pending_plan_revisions.pop(user_id, None)
    if pending is None:
        return False
    lang = await _lang(db, user_id)
    if isinstance(pending, PendingRename):
        await _rename(message, db, user_id, pending.plan_id, text, lang)
        return True
    if isinstance(pending, PendingNewPlan):
        await _generate(message, db, llm, user_id, lang, text)
        return True
    if isinstance(pending, PendingImport):
        await message.answer(t("plan.importing", lang))
        result = await planning.import_plan(db, llm, user_id, text)
        await _show_round(message, result, lang, title_key="plan.import_title")
        return True
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


async def _rename(
    message: Message, db: Database, user_id: int, plan_id: int, text: str, lang: str
) -> None:
    result = await planning.rename_plan(db, user_id, plan_id, text)
    status = result.status
    if status == planning.RenameStatus.OK:
        await message.answer(t("plan.renamed", lang, name=result.name))
    elif status == planning.RenameStatus.EMPTY:
        await message.answer(t("plan.rename_empty", lang))
    elif status == planning.RenameStatus.TOO_LONG:
        await message.answer(t("plan.rename_too_long", lang, max=NAME_MAX_LENGTH))
    elif status == planning.RenameStatus.FORBIDDEN:
        await message.answer(t("plan.rename_forbidden", lang, term=result.term))
    else:  # NOT_FOUND
        await message.answer(t("plan.not_found", lang))


def build_router() -> Router:
    router = Router(name="plan")
    router.message.register(cmd_plan, Command("plan"))
    router.callback_query.register(on_plan_menu, PlanMenu.filter())
    router.callback_query.register(on_plan_draft, PlanDraft.filter())
    return router
