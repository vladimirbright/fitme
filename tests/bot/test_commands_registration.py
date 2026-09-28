"""`set_my_commands` is called once per supported language at startup (A§6.2)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import SetMyCommands, TelegramMethod
from aiogram.methods.base import TelegramType
from conftest import TEST_TOKEN, FakeSession

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


class _UnauthorizedSession(BaseSession):
    """A session that rejects every call the way Telegram rejects an invalid bot token —
    used to prove `register_commands` degrades gracefully (M10: a bad/placeholder token must
    not crash `fitme serve` at startup, e.g. under Docker Compose's `restart: unless-stopped`)."""

    async def close(self) -> None:
        return None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError
        yield b""  # pragma: no cover


async def test_register_commands_logs_and_does_not_raise_on_an_invalid_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot(token=TEST_TOKEN, session=_UnauthorizedSession())

    with caplog.at_level(logging.WARNING):
        await register_commands(bot)  # must not raise

    assert "failed to register bot commands" in caplog.text
