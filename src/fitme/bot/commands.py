"""The bot command list, registered per supported language at startup (A§6.2: "Register the
command list with `setMyCommands` for each supported language.")."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import AiogramError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from fitme.i18n import supported_languages, t

_logger = logging.getLogger(__name__)

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
    language, scoped to private chats (the only kind this bot ever talks in, ADR 0002).

    A failure here is logged and swallowed rather than raised, the same "log, don't crash the
    process" choice `cli/commands.py::_retention_once` already makes for the daily retention
    job: this call is a one-time startup nicety (the command list shows up in Telegram's UI),
    not something the rest of `fitme serve` depends on. By the time this runs, `serve_async`
    has already confirmed the token itself is valid (its own `bot.me()` preflight check, M10
    review B4) — a rejected token never reaches this function at all, it makes `fitme serve`
    refuse to start with a clear message instead. What lands here is narrower: Telegram
    briefly unreachable, or some other transient hiccup on an otherwise-valid token, which
    must not take the whole process down over a command-list nicety."""
    scope = BotCommandScopeAllPrivateChats()
    for lang in supported_languages():
        try:
            await bot.set_my_commands(
                commands=commands_for_language(lang), scope=scope, language_code=lang
            )
        except AiogramError:
            _logger.warning("failed to register bot commands", extra={"language": lang})
            return  # further languages would fail the same way (same bot, same token)
