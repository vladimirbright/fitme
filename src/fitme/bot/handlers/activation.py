"""`/activate <code>` (A§6.1). `OwnerGateMiddleware` only lets this through while no account
is bound; the handler itself still re-checks (defense in depth) and does the actual bind."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from fitme.bot.handlers.setup import show_step
from fitme.db.connection import Database
from fitme.i18n import resolve_language
from fitme.i18n import t as translate
from fitme.services.identity import BindOutcome, bind_telegram_account
from fitme.services.profile import STEP_ORDER, set_step


async def cmd_activate(message: Message, command: CommandObject, db: Database) -> None:
    assert message.from_user is not None
    lang = resolve_language(message.from_user.language_code)
    args = (command.args or "").strip()
    if not args:
        await message.answer(translate("activation.usage", lang))
        return

    result = await bind_telegram_account(
        db,
        code=args,
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        language=lang,
    )

    if result.outcome is BindOutcome.ALREADY_BOUND:
        await message.answer(translate("activation.already_bound", lang))
        return
    if result.outcome is BindOutcome.TOO_MANY_ATTEMPTS:
        await message.answer(translate("activation.too_many_attempts", lang))
        return
    if result.outcome is BindOutcome.INVALID_CODE:
        await message.answer(translate("activation.invalid", lang))
        return

    assert result.user_id is not None
    await message.answer(translate("activation.success", lang))
    await set_step(db, result.user_id, STEP_ORDER[0])
    await show_step(message, db, result.user_id, STEP_ORDER[0])


def build_router() -> Router:
    router = Router(name="activation")
    router.message.register(cmd_activate, Command("activate"))
    return router
