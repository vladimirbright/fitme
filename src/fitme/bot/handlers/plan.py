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
from fitme.bot.waiting import waiting_message
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.models import NAME_MAX_LENGTH, Plan
from fitme.i18n import t
from fitme.services import conversations, planning
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
    """The new plan's description was asked for: asked when the user already has plans, so
    the new one isn't a near-copy of them. ADR 0017: the answer goes to the assistant in
    the new plan's planning session (opened with the question in its history), which asks
    a clarifying question when it must, or generates; this marker only keeps "Generate
    anyway" and Cancel valid."""


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


async def _session_minutes(db: Database, user_id: int) -> int | None:
    """The profile's session length: what a workout's estimate is compared with (ADR 0016)."""
    profile = (await profile_service.get_snapshot(db, user_id)).profile
    return None if profile is None else profile.session_minutes


async def _send_plan(
    message: Message,
    plan: Plan,
    *,
    title: str,
    lang: str,
    markup: InlineKeyboardMarkup | None,
    session_minutes: int | None,
    unmatched: Sequence[str] = (),
) -> None:
    text = rendering.render_plan_text(
        plan,
        catalog=load_catalog(),
        lang=lang,
        title=title,
        unmatched=unmatched,
        session_minutes=session_minutes,
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
    db: Database,
    user_id: int,
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
        session_minutes=await _session_minutes(db, user_id),
        unmatched=result.unmatched,
    )
    await _draft_in_session(
        db, user_id, plan_id=result.plan_id, round_=result, plan=plan, lang=lang
    )


async def _draft_in_session(
    db: Database,
    user_id: int,
    *,
    plan_id: int | None,
    round_: planning.PlanRoundResult,
    plan: Plan,
    lang: str,
) -> None:
    """ADR 0004: every shown draft is the current draft of the planning session for its plan
    (opened here if needed), and the session's history notes that it was shown."""
    record = await conversations.planning_draft_shown(
        db, user_id, plan_id=plan_id, draft_decision_id=round_.decision_id
    )
    await conversations.record_outgoing(
        db,
        user_id,
        record.id,
        t(
            "assistant.history_draft",
            lang,
            name=plan.name,
            workouts=len(plan.workouts),
            days=len(plan.schedule),
        ),
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
    # A short "generating" message and an animated placeholder under it while the model
    # works (deleted once it is done); the DB lock is never held across the call (A§4.6), so
    # other updates keep flowing meanwhile.
    await message.answer(t("plan.generating", lang))
    async with waiting_message(message):
        result = await planning.propose_new_plan(db, llm, user_id, guidance=guidance)
    await _show_round(message, result, lang, db=db, user_id=user_id)


async def _ask_or_generate(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    lang: str,
    pending_plan_revisions: PendingRevisions,
) -> None:
    """ "New plan": first ask what the new one should be — the answer arrives as free text
    (stop-word scan first, `free_text.py`) and goes to the planner in the new plan's session,
    which talks it through and stages the plan when the user agrees; "Generate anyway" runs
    the legacy generator from the profile instead. Experiment (llm-first planning): asked
    with no plans yet too, so a first plan is the one the user describes, not the profile's
    default."""
    if await planning.is_gated(db, user_id):
        # The gate refuses anyway: go straight to the round (which logs the refusal as
        # usual) instead of asking a question that leads nowhere.
        await _generate(message, db, llm, user_id, lang)
        return
    plans = await planning.list_plans(db, user_id)
    pending_plan_revisions[user_id] = PendingNewPlan()
    if plans:
        names = ", ".join(f"“{plan.name}”" for plan in plans)
        question = t("plan.new_guidance_prompt", lang, names=names)
    else:
        question = t("plan.new_first_prompt", lang)
    await message.answer(question, reply_markup=rendering.new_plan_guidance_markup(lang))
    # ADR 0017: the answer belongs to the new plan's session, and the assistant reading it
    # sees what it answers.
    record = await conversations.open_planning(db, user_id, None)
    await conversations.record_outgoing(db, user_id, record.id, question)


async def _send_detail(
    message: Message, detail: planning.PlanDetail, lang: str, session_minutes: int | None
) -> None:
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
        session_minutes=session_minutes,
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
    await _send_detail(message, detail, lang, await _session_minutes(db, user_id))


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


async def revise_from_base(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    base: planning.RevisionBase,
    text: str,
) -> None:
    """A redesign by the plan designer (`plan_revise`), shown as the session's new draft."""
    lang = await _lang(db, user_id)
    await message.answer(t("plan.revising", lang))
    try:
        async with waiting_message(message):
            result = await planning.revise_plan(db, llm, user_id, base, text)
    except planning.StaleDraftError:
        await message.answer(t("plan.stale_draft", lang))
        return
    except planning.PlanNotFoundError:
        await message.answer(t("plan.not_found", lang))
        return
    await _show_round(message, result, lang, db=db, user_id=user_id)


async def show_draft(
    message: Message, db: Database, user_id: int, decision_id: int, lang: str
) -> None:
    """The whole current draft, with the session buttons."""
    if await planning.current_draft_id(db, user_id) != decision_id:
        await message.answer(t("plan.stale_draft", lang))
        return
    plan = await planning.draft_base_plan(db, user_id, plan_id=None, draft_decision_id=decision_id)
    if plan is None:
        await message.answer(t("plan.stale_draft", lang))
        return
    await _send_plan(
        message,
        plan,
        title=t("plan.draft_title", lang),
        lang=lang,
        markup=rendering.session_draft_markup(decision_id, lang),
        session_minutes=await _session_minutes(db, user_id),
    )


async def show_staged_draft(
    message: Message,
    db: Database,
    user_id: int,
    result: planning.EditDraftResult,
    lang: str,
) -> None:
    """A whole plan the planner staged (`stage_plan`): shown whole like a generated draft,
    with Confirm / Change / Cancel, then any load the guards limited; the session's history
    notes it was shown (`services.assistant` already made it the session's draft). When the
    same turn saved it (the user said "save it"), the session is closed: the plan is shown
    without buttons, and the saved notice follows. No session-length comparison: the
    profile's session length is a default, and the planner's plan has the length the user
    asked for (a 90-minute class)."""
    round_ = result.round
    if round_ is None or round_.plan is None:
        lines = [t("assistant.blocked", lang), *(f"• {e}" for e in result.errors)]
        await message.answer("\n".join(lines))
        return
    plan = round_.plan
    session = await conversations.current_planning(db, user_id)
    is_open = session is not None and session.draft_decision_id == round_.decision_id
    # A draft that replaces a saved plan says which one.
    replaces = (
        None
        if round_.plan_id is None
        else await planning.get_plan_detail(db, user_id, round_.plan_id)
    )
    title = (
        t("plan.draft_title", lang)
        if replaces is None
        else t("plan.changes_draft_title", lang, name=replaces.record.name)
    )
    await _send_plan(
        message,
        plan,
        title=title,
        lang=lang,
        markup=rendering.draft_markup(round_.decision_id, lang) if is_open else None,
        session_minutes=None,
    )
    if result.requested is not None:
        notes = rendering.load_change_notes(result.requested, plan, load_catalog(), lang)
        if notes:
            await message.answer("\n".join(notes))
    if session is not None and is_open:
        await conversations.record_outgoing(
            db,
            user_id,
            session.id,
            t(
                "assistant.history_draft",
                lang,
                name=plan.name,
                workouts=len(plan.workouts),
                days=len(plan.schedule),
            ),
        )


async def show_edit_draft(
    message: Message,
    result: planning.EditDraftResult,
    before: Plan | None,
    lang: str,
) -> None:
    """A draft built from direct edits (ADR 0004): what changed against the previous draft
    (or the saved plan), any load the guards limited, and Save / whole draft / Close."""
    round_ = result.round
    if round_ is None or round_.plan is None:
        lines = [t("assistant.blocked", lang), *(f"• {e}" for e in result.errors)]
        await message.answer("\n".join(lines))
        return
    catalog = load_catalog()
    plan = round_.plan
    lines = [t("assistant.draft_updated", lang, name=plan.name)]
    if before is not None:
        lines.extend(rendering.plan_diff_lines(before, plan, catalog, lang))
    if result.requested is not None:
        lines.extend(rendering.load_change_notes(result.requested, plan, catalog, lang))
    lines.extend(["", t("assistant.draft_hint", lang)])
    await message.answer(
        "\n".join(lines), reply_markup=rendering.session_draft_markup(round_.decision_id, lang)
    )


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
        await _send_detail(message, detail, lang, await _session_minutes(db, user_id))
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
        # Experiment (llm-first planning): the answer goes to the planner, in this plan's
        # planning session (opened here, with the prompt in its history), not to the legacy
        # `plan_revise` designer.
        await query.answer()
        pending_plan_revisions.pop(user_id, None)
        revise_of = t("plan.revise_of", lang, name=detail.record.name)
        change_prompt = t("plan.change_prompt", lang)
        await message.answer(revise_of)
        await message.answer(change_prompt, reply_markup=rendering.cancel_revision_markup(0, lang))
        record = await conversations.open_planning(db, user_id, detail.record.id)
        await conversations.record_outgoing(db, user_id, record.id, f"{revise_of}\n{change_prompt}")
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
            await conversations.close_planning(db, user_id, status="discarded")
            await message.answer(t("plan.new_cancelled", lang))
            return
        current = await planning.current_draft_id(db, user_id)
        closed = await conversations.close_planning(db, user_id, status="discarded")
        if pending is not None or closed is not None or (decision_id and current == decision_id):
            await message.answer(t("plan.change_cancelled", lang))
        else:
            await message.answer(t("plan.nothing_to_cancel", lang))
        return

    if action == "confirm":
        # No staleness pre-check here: `confirm_plan` decides in its own transaction, and it
        # answers a double-tap with ALREADY_SAVED before the (now stale) draft check.
        pending_plan_revisions.pop(user_id, None)
        result = await planning.confirm_plan(db, settings, user_id, decision_id)
        if result.status in (planning.ConfirmStatus.SAVED, planning.ConfirmStatus.ALREADY_SAVED):
            await conversations.close_planning(db, user_id, status="saved")
        await _show_confirm(query, message, db, user_id, result, lang)
        return

    if await planning.current_draft_id(db, user_id) != decision_id:
        await query.answer(t("plan.stale_draft", lang), show_alert=True)
        return

    if action == "view":
        await query.answer()
        await show_draft(message, db, user_id, decision_id, lang)
        return

    if action == "change":
        # Experiment (llm-first planning): no pending revision — the next free text reaches
        # the planner, which sees this draft as `state.planning_session.draft`.
        await query.answer()
        pending_plan_revisions.pop(user_id, None)
        change_prompt = t("plan.change_prompt", lang)
        await message.answer(
            change_prompt, reply_markup=rendering.cancel_revision_markup(decision_id, lang)
        )
        # The draft is the session's current one again, even if its session idled out.
        session = await conversations.planning_draft_shown(
            db,
            user_id,
            plan_id=await planning.draft_plan_id(db, user_id, decision_id),
            draft_decision_id=decision_id,
        )
        await conversations.record_outgoing(db, user_id, session.id, change_prompt)
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
    if a revision, an import (M8b) or a rename was pending and the text was consumed by it.
    A new plan's description is not consumed: it goes on to the assistant (ADR 0017)."""
    pending = pending_plan_revisions.pop(user_id, None)
    if pending is None:
        return False
    lang = await _lang(db, user_id)
    if isinstance(pending, PendingRename):
        await _rename(message, db, user_id, pending.plan_id, text, lang)
        return True
    if isinstance(pending, PendingNewPlan):
        # ADR 0017: not consumed here — the free-text handler passes it on to the assistant,
        # in the new plan's session opened with the question.
        return False
    if isinstance(pending, PendingImport):
        await message.answer(t("plan.importing", lang))
        async with waiting_message(message):
            result = await planning.import_plan(db, llm, user_id, text)
        await _show_round(
            message, result, lang, db=db, user_id=user_id, title_key="plan.import_title"
        )
        return True
    await revise_from_base(message, db, llm, user_id, pending.base, text)
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
