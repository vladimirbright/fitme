"""The bot command list, registered per supported language at startup (A§6.2: "Register the
command list with `setMyCommands` for each supported language.")."""

from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from fitme.i18n import supported_languages, t

COMMAND_NAMES: tuple[str, ...] = (
    "start",
    "help",
    "profile",
    "plan",
    "train",
    "stats",
    "system",
    "export",
    "delete",
    "cancel",
)


def commands_for_language(lang: str) -> list[BotCommand]:
    return [
        BotCommand(command=name, description=t(f"commands.{name}", lang)) for name in COMMAND_NAMES
    ]


async def register_commands(bot: Bot) -> None:
    """Called once at `fitme serve` startup: one `setMyCommands` call per supported
    language, scoped to private chats (the only kind this bot ever talks in, ADR 0002)."""
    scope = BotCommandScopeAllPrivateChats()
    for lang in supported_languages():
        await bot.set_my_commands(
            commands=commands_for_language(lang), scope=scope, language_code=lang
        )
