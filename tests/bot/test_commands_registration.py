"""`set_my_commands` is called once per supported language at startup (A§6.2)."""

from __future__ import annotations

from aiogram import Bot
from aiogram.methods import SetMyCommands
from conftest import FakeSession

from fitme.bot.commands import register_commands
from fitme.i18n import supported_languages


async def test_set_my_commands_is_called_per_language(bot: Bot, session: FakeSession) -> None:
    await register_commands(bot)

    calls = [m for m in session.sent if isinstance(m, SetMyCommands)]
    languages = {call.language_code for call in calls}
    assert languages == set(supported_languages())
    for call in calls:
        command_names = {c.command for c in call.commands}
        assert {"start", "help", "profile", "export", "delete", "cancel"} <= command_names
