"""A placeholder message that animates while the bot waits for a model, then becomes the
reply: `...🏃...` → `..🏃....` → ... by editing the same message, and the answer is written
into it when it is ready.

Telegram documents no exact limit for edits; the practical one is about one message per
second per chat, edits included, with a 429 (`TelegramRetryAfter`) past it. So a frame is
shown every `FRAME_INTERVAL` seconds, a 429 is waited out, and any other Telegram error stops
the animation (the placeholder stays; the reply then goes out as a new message). The frames
are emoji and dots only: nothing to translate.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

FRAME_INTERVAL = 1.5
TRACK_LENGTH = 7
RUNNERS = ("🏃", "🚴", "🏊")


def frames(runner: str, length: int = TRACK_LENGTH) -> list[str]:
    """One lap, right to left (the runners face left): the runner at each position of a
    track of `length` dots."""
    return [
        "." * position + runner + "." * (length - position) for position in range(length, -1, -1)
    ]


class WaitingMessage:
    def __init__(self, message: Message, interval: float) -> None:
        self._message = message
        self._interval = interval
        self._placeholder: Message | None = None
        self._task: asyncio.Task[None] | None = None
        self._finished = False

    async def start(self) -> None:
        laps = frames(random.choice(RUNNERS))
        try:
            self._placeholder = await self._message.answer(laps[0])
        except TelegramAPIError:
            return
        self._task = asyncio.create_task(self._animate(self._placeholder, laps))

    async def _animate(self, placeholder: Message, laps: list[str]) -> None:
        index = 0
        while True:
            await asyncio.sleep(self._interval)
            index = (index + 1) % len(laps)
            try:
                await placeholder.edit_text(laps[index])
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
            except TelegramAPIError:
                return

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def finish(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Stop the animation and write `text` into the placeholder (a new message if it
        can't be edited)."""
        await self.stop()
        self._finished = True
        if self._placeholder is not None:
            try:
                await self._placeholder.edit_text(text, reply_markup=reply_markup)
                return
            except TelegramAPIError:
                pass
        await self._message.answer(text, reply_markup=reply_markup)

    async def close(self) -> None:
        """Stop the animation; a placeholder that never got a reply is deleted."""
        await self.stop()
        if self._finished or self._placeholder is None:
            return
        self._finished = True
        with contextlib.suppress(TelegramAPIError):
            await self._placeholder.delete()


@contextlib.asynccontextmanager
async def waiting_message(
    message: Message, *, interval: float = FRAME_INTERVAL
) -> AsyncIterator[WaitingMessage]:
    """Animate a placeholder in `message`'s chat for the body of the `async with`; call
    `finish` with the reply, or the placeholder is deleted on exit."""
    waiting = WaitingMessage(message, interval)
    await waiting.start()
    try:
        yield waiting
    finally:
        await waiting.close()
