"""`/start`, `/help`, `/cancel` (A§6.2). Always run for the bound owner: `OwnerGateMiddleware`
never lets these through for anyone else."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from fitme.bot.commands import COMMAND_NAMES
from fitme.bot.handlers.plan import PendingImport, PendingRevisions
from fitme.bot.handlers.setup import show_step
from fitme.bot.handlers.train import PendingTrains, handle_cancel, show_pending_recap
from fitme.bot.keyboards import hold_clear_markup
from fitme.db.connection import Database
from fitme.i18n import t
from fitme.services import profile as profile_service
from fitme.services import safety
from fitme.services.llm_runtime import LlmRuntime


async def cmd_start(message: Message, db: Database, llm: LlmRuntime, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language

    for hold in await safety.open_holds(db, user_id):
        if await safety.can_clear(db, hold):
            await message.answer(
                t("hold_clear.question", lang), reply_markup=hold_clear_markup(hold.id, lang)
            )
        else:
            await message.answer(t("hold.still_open", lang))

    step = await profile_service.get_step(db, user_id)
    if step is not None:
        await show_step(message, db, user_id, step)
        return

    if snapshot.profile is None or snapshot.profile.completed_at is None:
        await message.answer(t("start.bound_no_profile", lang))
        first_step = profile_service.STEP_ORDER[0]
        await profile_service.set_step(db, user_id, first_step)
        await show_step(message, db, user_id, first_step)
        return

    await message.answer(t("start.bound_with_profile", lang))
    await show_pending_recap(message, db, llm, user_id, lang)


async def cmd_help(message: Message, db: Database, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    lines = [t("help.intro", lang), ""]
    lines.extend(f"/{name} — {t(f'commands.{name}', lang)}" for name in COMMAND_NAMES)
    lines.append("")
    lines.append(t("disclosure.ai", lang))
    await message.answer("\n".join(lines))


async def cmd_cancel(
    message: Message,
    db: Database,
    user_id: int,
    pending_deletes: set[int],
    pending_train: PendingTrains,
    pending_plan_revisions: PendingRevisions,
) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    was_deleting = user_id in pending_deletes
    pending_deletes.discard(user_id)
    # A pending /plan prompt (a revision's "what should change?", or M8b's "paste your
    # program") dies with /cancel: the next free text must not reach the LLM as one.
    pending_plan = pending_plan_revisions.pop(user_id, None)
    step = await profile_service.get_step(db, user_id)

    if await handle_cancel(message, db, user_id, pending_train):
        # A§6.2: an in-progress workout is kept (resumable); an unstarted one is aborted.
        # `cancel_train` has already said which.
        return
    if pending_plan is not None:
        key = (
            "plan.paste_cancelled"
            if isinstance(pending_plan, PendingImport)
            else "plan.change_cancelled"
        )
        await message.answer(t(key, lang))
        return
    if step is not None:
        # Setup progress is never discarded by /cancel (A§5.1): it's still there to resume.
        await message.answer(t("cancel.setup_paused", lang))
    elif was_deleting:
        await message.answer(t("cancel.done", lang))
    else:
        await message.answer(t("cancel.nothing_active", lang))


def build_router() -> Router:
    router = Router(name="start")
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    return router
