"""Commands that belong to later milestones (A§6.2): `/plan` (M6), `/train` (M7), `/stats`
and `/system` (M9). Each replies with a short localized "not available yet" message so the
command list registered with `setMyCommands` always has a working handler behind it."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from fitme.db.connection import Database
from fitme.i18n import t
from fitme.services import profile as profile_service


async def cmd_not_available_yet(message: Message, db: Database, user_id: int) -> None:
    snapshot = await profile_service.get_snapshot(db, user_id)
    await message.answer(t("stub.not_available_yet", snapshot.language))


def build_router() -> Router:
    router = Router(name="stubs")
    router.message.register(cmd_not_available_yet, Command("plan", "train", "t", "stats", "system"))
    return router
