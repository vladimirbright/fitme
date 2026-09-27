"""`/export`, `/delete` (A§6.2, A§8.3) and the hold-clearing button (A§6.6).

`/export` and `/delete` are wired to `services/account.py` now, cheaply, ahead of the later
milestones that build `/plan`/`/train` (AGENTS.md §5: export/delete from day one). `/delete`
is the two-step confirmation from A§6.2: a button, then typing `DELETE`. The "typing DELETE"
half is handled in `bot/handlers/free_text.py`, which checks the `pending_deletes` set this
module's button handler populates.
"""

from __future__ import annotations

import json

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from fitme.bot.callback_data import DeleteConfirm, HoldClear
from fitme.bot.keyboards import delete_prompt_markup
from fitme.db.connection import Database
from fitme.i18n import t
from fitme.services import account as account_service
from fitme.services import profile as profile_service
from fitme.services import safety


async def cmd_export(message: Message, db: Database, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    data = await account_service.export_user(db, user_id)
    payload = json.dumps(data, indent=2, default=str).encode("utf-8")
    document = BufferedInputFile(payload, filename="fitme-export.json")
    await message.answer_document(document, caption=t("export.caption", lang))


async def cmd_delete(message: Message, db: Database, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    await message.answer(t("delete.prompt", lang), reply_markup=delete_prompt_markup(lang))


async def on_delete_confirm(
    query: CallbackQuery,
    callback_data: DeleteConfirm,
    db: Database,
    user_id: int,
    pending_deletes: set[int],
) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    message = query.message
    assert message is not None
    if callback_data.action == "start":
        pending_deletes.add(user_id)
        await message.edit_text(t("delete.type_confirm", lang))  # type: ignore[union-attr]
    else:
        pending_deletes.discard(user_id)
        await message.edit_text(t("delete.cancelled", lang))  # type: ignore[union-attr]
    await query.answer()


async def on_hold_clear(
    query: CallbackQuery, callback_data: HoldClear, db: Database, user_id: int
) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    message = query.message
    assert message is not None
    holds = {hold.id: hold for hold in await safety.open_holds(db, user_id)}
    hold = holds.get(callback_data.hold_id)
    if hold is None:
        await query.answer(t("errors.stale_callback", lang))
        return
    if not await safety.can_clear(db, hold):
        await query.answer(t("hold.too_soon", lang), show_alert=True)
        return
    await safety.clear(db, user_id=user_id, hold_id=hold.id)
    await message.edit_text(t("hold.cleared", lang))  # type: ignore[union-attr]
    await query.answer()


def build_router() -> Router:
    router = Router(name="account")
    router.message.register(cmd_export, Command("export"))
    router.message.register(cmd_delete, Command("delete"))
    router.callback_query.register(on_delete_confirm, DeleteConfirm.filter())
    router.callback_query.register(on_hold_clear, HoldClear.filter())
    return router
