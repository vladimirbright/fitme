"""`/train`, `/t` (A§6.2, A§6.5): the workout loop over Telegram buttons.

Everything that decides anything lives in `services.training` (the state machine is in
`workout_sessions`, so a restart resumes exactly). This module routes buttons and text,
renders through `bot.train_rendering` and i18n, and keeps one piece of transient UI state:
`pending_train` (per user, which free-text prompt — an adjustment request or a block's result
entry — is waiting). It's in-memory dispatcher data like `pending_plan_revisions`: a restart
forgets the prompt, and the user taps the button again.

Free text arrives through `bot/handlers/free_text.py`, which saves it and runs the stop-word
guard first (A§6.3; a hit halts the active session through `services.safety.halt` and never
reaches here). `handle_train_text` then routes it to `adjust` or `parse_results`, which run
their own stop-word scan again before any LLM call (defense in depth).

Stale buttons (A§6.3): every `TrainAction` carries the session id, the block and the item;
`services.training` checks them against the session's current status and block and answers
a mismatch with `STALE`, which becomes a toast here. The ⚠ button is the one exception: it
always halts (A§6.3), stale or not.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from fitme.bot import plan_rendering
from fitme.bot import train_rendering as rendering
from fitme.bot.callback_data import TrainAction, TrainPick
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.enums import WorkoutSessionStatus
from fitme.domain.models import Refusal
from fitme.i18n import t
from fitme.services import profile as profile_service
from fitme.services import training
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.safety import HaltResult


@dataclass(frozen=True, slots=True)
class PendingTrain:
    kind: str  # "adjust" | "results"
    session_id: int
    block: int = 0
    item: int = 0


PendingTrains = dict[int, PendingTrain]

_STATUS_IN_PROGRESS = WorkoutSessionStatus.IN_PROGRESS.value
_STATUS_DRAFT = WorkoutSessionStatus.DRAFT.value
_STATUS_CONFIRMED = WorkoutSessionStatus.CONFIRMED.value


async def _lang(db: Database, user_id: int) -> str:
    return (await profile_service.get_snapshot(db, user_id)).language


def _message_of(query: CallbackQuery) -> Message:
    message = query.message
    assert isinstance(message, Message)  # a stranger can't reach a bot keyboard (A§6.1)
    return message


async def _send_long(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    chunks = plan_rendering.split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=markup)


async def _send_refusal(message: Message, refusal: Refusal, lang: str) -> None:
    # AGENTS.md §2: a refusal is a valid output. Shown via the per-code i18n copy, never
    # model text.
    await message.answer(t(f"refusal.{refusal.code.value}", lang))


async def _send_halt(message: Message, _halt: HaltResult, lang: str) -> None:
    # A§6.6 step 4: the fixed, non-LLM message; no alternatives, no lighter option.
    await message.answer(t("halt.message", lang))


# --- Rendering steps ---------------------------------------------------------------------------


async def _show_precheck(message: Message, session_id: int, lang: str) -> None:
    await message.answer(
        t("precheck.question", lang), reply_markup=rendering.precheck_markup(session_id, lang)
    )


async def _show_review(message: Message, view: training.ReviewView, lang: str) -> None:
    await _send_long(
        message,
        rendering.review_text(view, catalog=load_catalog(), lang=lang),
        rendering.review_markup(view.session_id, adjusted=view.adjusted, lang=lang),
    )


async def _show_block(message: Message, view: training.BlockView, lang: str) -> None:
    await _send_long(
        message,
        rendering.block_text(view, catalog=load_catalog(), lang=lang),
        rendering.block_markup(view, lang),
    )


async def _show_advance(message: Message, advance: training.Advance, lang: str) -> None:
    if advance.next_block is not None:
        await _show_block(message, advance.next_block, lang)
        return
    completion = advance.completion
    assert completion is not None
    # M8 hook: the recap, the check-ins and the progression follow this message.
    await message.answer(
        t(
            "train.completed",
            lang,
            logged=completion.sets_logged,
            planned=completion.sets_planned,
        )
    )


async def _show_suggestion(
    message: Message, suggested: training.WorkoutSuggested, lang: str
) -> None:
    await message.answer(
        rendering.suggestion_text(
            suggested.plan, suggested.workout, scheduled_today=suggested.scheduled_today, lang=lang
        ),
        reply_markup=rendering.suggestion_markup(
            suggested.plan.id, suggested.workout, has_others=bool(suggested.others), lang=lang
        ),
    )


async def _show_active(
    message: Message,
    db: Database,
    settings: Settings,
    user_id: int,
    active: training.ActiveSession,
    lang: str,
) -> None:
    """Continue an unfinished session where it is (A§3, A§6.5): the precheck for a `draft`,
    the review for a `confirmed` one, a Resume prompt for one `in_progress`."""
    session = active.session
    if session.status == _STATUS_DRAFT:
        await _show_precheck(message, session.id, lang)
        return
    if session.status == _STATUS_CONFIRMED:
        result = await training.review(db, settings, user_id, session.id)
        if result.review is not None:
            await _show_review(message, result.review, lang)
        elif result.refusal is not None:
            await _send_refusal(message, result.refusal, lang)
        return
    await message.answer(
        t(
            "train.active_prompt",
            lang,
            workout=rendering.workout_label(active.workout),
            block=session.current_block + 1,
            total=active.total_blocks,
        ),
        reply_markup=rendering.resume_markup(session.id, lang),
    )


async def _ask_results(
    message: Message, prompt: training.ResultPrompt, lang: str, pending: PendingTrains, user_id: int
) -> None:
    pending[user_id] = PendingTrain(
        kind="results", session_id=prompt.session_id, block=prompt.block, item=prompt.item
    )
    await message.answer(
        rendering.results_prompt_text(prompt, lang),
        reply_markup=rendering.results_pending_markup(prompt.session_id, prompt.block, lang),
    )


# --- /train -----------------------------------------------------------------------------------


async def cmd_train(message: Message, db: Database, settings: Settings, user_id: int) -> None:
    lang = await _lang(db, user_id)
    result = await training.entry(db, user_id)
    if isinstance(result, training.Refused):
        await _send_refusal(message, result.refusal, lang)
    elif isinstance(result, training.NoPlan):
        await message.answer(t("train.no_plan", lang))
    elif isinstance(result, training.ChoosePlan):
        await message.answer(
            t("train.choose_plan", lang), reply_markup=rendering.plan_choice_markup(result.plans)
        )
    elif isinstance(result, training.WorkoutSuggested):
        await _show_suggestion(message, result, lang)
    else:
        await _show_active(message, db, settings, user_id, result, lang)


async def on_train_pick(
    query: CallbackQuery,
    callback_data: TrainPick,
    db: Database,
    settings: Settings,
    user_id: int,
    pending_train: PendingTrains,
) -> None:
    lang = await _lang(db, user_id)
    message = _message_of(query)
    kind = callback_data.kind

    if kind == "plan":
        suggested = await training.suggest_workout(db, user_id, callback_data.plan_id)
        if suggested is None:
            await query.answer(t("errors.stale_callback", lang))
            return
        await query.answer()
        await _show_suggestion(message, suggested, lang)
        return

    if kind == "workouts":
        suggested = await training.suggest_workout(db, user_id, callback_data.plan_id)
        if suggested is None:
            await query.answer(t("errors.stale_callback", lang))
            return
        await query.answer()
        workouts = [suggested.workout, *suggested.others]
        await message.answer(
            t("train.choose_workout", lang),
            reply_markup=rendering.workout_choice_markup(callback_data.plan_id, workouts),
        )
        return

    if kind == "workout":
        created = await training.create_session(
            db, user_id, callback_data.plan_id, callback_data.key
        )
        if created is None:
            await query.answer(t("errors.stale_callback", lang))
            return
        await query.answer()
        pending_train.pop(user_id, None)
        if isinstance(created, training.Refused):
            await _send_refusal(message, created.refusal, lang)
        elif isinstance(created, training.ActiveSession):
            await _show_active(message, db, settings, user_id, created, lang)
        else:
            await _show_precheck(message, created.session.id, lang)
        return

    await query.answer(t("errors.stale_callback", lang))


# --- Session buttons --------------------------------------------------------------------------


async def on_train_action(
    query: CallbackQuery,
    callback_data: TrainAction,
    db: Database,
    settings: Settings,
    user_id: int,
    pending_train: PendingTrains,
) -> None:
    lang = await _lang(db, user_id)
    message = _message_of(query)
    action = callback_data.action
    session_id = callback_data.session_id
    block = callback_data.block

    if action == "pain":
        # A§6.3: always the halt path, whatever the state of the button's session.
        await query.answer()
        pending_train.pop(user_id, None)
        halted = await training.pain_button(db, user_id, session_id, block)
        await _send_halt(message, halted, lang)
        return

    if action == "precheck_yes":
        await query.answer()
        pending_train.pop(user_id, None)
        result = await training.precheck_yes(db, user_id, session_id)
        if result.halt is not None:
            await _send_halt(message, result.halt, lang)
        else:
            await message.answer(t("train.stale", lang))
        return

    if action == "precheck_no":
        result = await training.precheck_no(db, settings, user_id, session_id)
        await _answer_review_result(query, message, result, lang)
        return

    if action == "adjust":
        result = await training.review(db, settings, user_id, session_id)
        if result.status != training.Status.OK:
            await query.answer(t("train.stale", lang), show_alert=True)
            return
        await query.answer()
        pending_train[user_id] = PendingTrain(kind="adjust", session_id=session_id)
        await message.answer(
            t("train.adjust_prompt", lang), reply_markup=rendering.abort_markup(session_id, lang)
        )
        return

    if action == "save_plan":
        saved = await training.save_to_plan(db, settings, user_id, session_id)
        if saved.status == training.Status.OK:
            await query.answer()
            await message.answer(
                t("train.saved_to_plan", lang, name=saved.plan_name, version=saved.version)
            )
        elif saved.status == training.Status.ALREADY:
            await query.answer(t("train.already_saved_to_plan", lang))
        elif saved.status == training.Status.REFUSED:
            await query.answer()
            assert saved.refusal is not None
            await _send_refusal(message, saved.refusal, lang)
        elif saved.status == training.Status.STARTED:
            await query.answer(t("train.already_started", lang), show_alert=True)
        else:
            await query.answer(t("train.nothing_to_save", lang), show_alert=True)
        return

    if action == "start":
        pending_train.pop(user_id, None)
        started = await training.start(db, settings, user_id, session_id)
        if started.status in (training.Status.OK, training.Status.ALREADY):
            await query.answer()
            assert started.block is not None
            await _show_block(message, started.block, lang)
        elif started.status == training.Status.REFUSED:
            await query.answer()
            assert started.refusal is not None
            await _send_refusal(message, started.refusal, lang)
        else:
            await query.answer(t("train.stale", lang), show_alert=True)
        return

    if action == "resume":
        pending_train.pop(user_id, None)
        resumed = await training.current_block(db, user_id, session_id)
        if resumed.block is None:
            await query.answer(t("train.stale", lang), show_alert=True)
            return
        await query.answer()
        await _show_block(message, resumed.block, lang)
        return

    if action == "abort":
        pending_train.pop(user_id, None)
        if await training.abort(db, user_id, session_id):
            await query.answer()
            await message.answer(t("train.aborted", lang))
        else:
            await query.answer(t("train.nothing_to_abort", lang))
        return

    if action == "done":
        pending_train.pop(user_id, None)
        advance = await training.complete_block_as_planned(db, user_id, session_id, block)
        await _answer_advance(query, message, advance, lang)
        return

    if action == "skip":
        pending_train.pop(user_id, None)
        advance = await training.skip_block(db, user_id, session_id, block)
        await _answer_advance(query, message, advance, lang, toast_key="train.block_skipped")
        return

    if action == "enter":
        prompt = await training.next_result_prompt(db, user_id, session_id, block)
        if prompt is None:
            await query.answer(t("train.stale", lang), show_alert=True)
            return
        await query.answer()
        await _ask_results(message, prompt, lang, pending_train, user_id)
        return

    if action == "parse_fix":
        prompt = await training.next_result_prompt(db, user_id, session_id, block)
        if prompt is None or prompt.item != callback_data.item:
            await query.answer(t("train.stale", lang), show_alert=True)
            return
        await query.answer()
        await _ask_results(message, prompt, lang, pending_train, user_id)
        return

    if action == "parse_ok":
        pending_train.pop(user_id, None)
        confirmed = await training.confirm_results(
            db, user_id, session_id, block, callback_data.item, callback_data.decision_id
        )
        if confirmed.status != training.Status.OK:
            await query.answer(t("train.stale", lang), show_alert=True)
            return
        await query.answer(t("train.block_logged", lang))
        if confirmed.next_prompt is not None:
            await _ask_results(message, confirmed.next_prompt, lang, pending_train, user_id)
        elif confirmed.advance is not None:
            await _show_advance(message, confirmed.advance, lang)
        return

    await query.answer(t("errors.stale_callback", lang))


async def _answer_review_result(
    query: CallbackQuery, message: Message, result: training.PrecheckResult, lang: str
) -> None:
    if result.review is not None:
        await query.answer()
        await _show_review(message, result.review, lang)
    elif result.refusal is not None:
        await query.answer()
        await _send_refusal(message, result.refusal, lang)
    else:
        await query.answer(t("train.stale", lang), show_alert=True)


async def _answer_advance(
    query: CallbackQuery,
    message: Message,
    advance: training.Advance,
    lang: str,
    *,
    toast_key: str = "train.block_logged",
) -> None:
    if advance.status != training.Status.OK:
        await query.answer(t("train.stale", lang), show_alert=True)
        return
    await query.answer(t(toast_key, lang))
    await _show_advance(message, advance, lang)


# --- Free text --------------------------------------------------------------------------------


async def handle_train_text(
    message: Message,
    db: Database,
    llm: LlmRuntime,
    user_id: int,
    text: str,
    pending_train: PendingTrains,
) -> bool:
    """Called by the free-text handler *after* the stop-word scan (A§6.3). Returns `True` if
    a `/train` prompt was pending and the text was consumed by it."""
    pending = pending_train.pop(user_id, None)
    if pending is None:
        return False
    lang = await _lang(db, user_id)

    if pending.kind == "adjust":
        await message.answer(t("train.adjusting", lang))
        result = await training.adjust(db, llm, user_id, pending.session_id, text)
        if result.halt is not None:
            await _send_halt(message, result.halt, lang)
            return True
        if result.review is not None:
            await _show_review(message, result.review, lang)
            return True
        if result.refusal is not None:
            await _send_refusal(message, result.refusal, lang)
            current = await training.review(db, llm.settings, user_id, pending.session_id)
            if current.review is not None:
                await _show_review(message, current.review, lang)
            return True
        await message.answer(t("train.stale", lang))
        return True

    parsed = await training.parse_results(
        db, llm, user_id, pending.session_id, pending.block, pending.item, text
    )
    status = parsed.status
    if status == training.ParseStatus.HALTED:
        # `halt` is None when ⚠ got there first; the fixed message is still the right reply.
        await message.answer(t("halt.message", lang))
        return True
    if status == training.ParseStatus.STALE or parsed.prompt is None:
        await message.answer(t("train.stale", lang))
        return True
    if status == training.ParseStatus.UNAVAILABLE:
        assert parsed.refusal is not None
        await _send_refusal(message, parsed.refusal, lang)
        pending_train[user_id] = pending
        return True
    if status == training.ParseStatus.UNCLEAR:
        pending_train[user_id] = pending
        await message.answer(
            rendering.results_unclear_text(parsed.prompt, lang),
            reply_markup=rendering.results_pending_markup(pending.session_id, pending.block, lang),
        )
        return True
    if status == training.ParseStatus.IMPLAUSIBLE:
        pending_train[user_id] = pending
        await message.answer(
            rendering.results_implausible_text(parsed.prompt, lang),
            reply_markup=rendering.results_pending_markup(pending.session_id, pending.block, lang),
        )
        return True
    assert parsed.parsed is not None and parsed.decision_id is not None
    await message.answer(
        rendering.parsed_text(parsed.prompt, parsed.parsed, lang),
        reply_markup=rendering.parsed_markup(parsed.prompt, parsed.decision_id, lang),
    )
    return True


async def handle_cancel(
    message: Message, db: Database, user_id: int, pending_train: PendingTrains
) -> bool:
    """`/cancel` (A§6.2): drops a pending prompt; an unstarted session (precheck or review)
    is aborted, an `in_progress` one is kept and can be resumed — the user picks **Abort
    workout** to abort it. Returns `True` if there was anything `/train`-related to cancel."""
    had_pending = pending_train.pop(user_id, None) is not None
    active = await training.active_session(db, user_id)
    if active is None:
        return had_pending
    lang = await _lang(db, user_id)
    if active.session.status == _STATUS_IN_PROGRESS:
        await message.answer(
            t("train.kept_after_cancel", lang),
            reply_markup=rendering.abort_markup(active.session.id, lang),
        )
        return True
    await training.abort(db, user_id, active.session.id)
    await message.answer(t("train.aborted", lang))
    return True


def build_router() -> Router:
    router = Router(name="train")
    router.message.register(cmd_train, Command("train", "t"))
    router.callback_query.register(on_train_pick, TrainPick.filter())
    router.callback_query.register(on_train_action, TrainAction.filter())
    return router
