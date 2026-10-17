"""A placeholder message that animates while the bot waits for a model, then becomes the
reply: `...🏃...` → `..🏃....` → ... by editing the same message, and the answer is written
into it when it is ready.

Telegram documents no exact limit for edits; the practical one is about one message per
second per chat, edits included, with a 429 (`TelegramRetryAfter`) past it. So a frame is
shown every `FRAME_INTERVAL` seconds, a 429 is waited out, and any other Telegram error stops
the animation. The reply is what matters: it waits out a 429 (`DELIVERY_ATTEMPTS` tries), and
if the placeholder can't be edited it goes out as a new message and the placeholder is
deleted. The frames are emoji and dots only: nothing to translate.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

FRAME_INTERVAL = 1.5
TRACK_LENGTH = 7
STOP_TIMEOUT = 10.0
DELIVERY_ATTEMPTS = 3
RUNNERS = ("🏃", "🚴", "🏊")


def frames(runner: str, length: int = TRACK_LENGTH) -> list[str]:
    """One lap, right to left (the runners face left): the runner at each position of a
    track of `length` dots."""
    return [
        "." * position + runner + "." * (length - position) for position in range(length, -1, -1)
    ]


async def with_retries[T](send: Callable[[], Awaitable[T]]) -> T:
    """`send()`, waiting out a 429 up to `DELIVERY_ATTEMPTS` times; the last 429 is raised."""
    for _ in range(DELIVERY_ATTEMPTS - 1):
        try:
            return await send()
        except TelegramRetryAfter as error:
            await pause(error.retry_after)
    return await send()


async def pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


class WaitingMessage:
    def __init__(self, message: Message, interval: float) -> None:
        self._message = message
        self._interval = interval
        self._placeholder: Message | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._finished = False

    async def start(self) -> None:
        laps = frames(random.choice(RUNNERS))
        try:
            self._placeholder = await self._message.answer(laps[0])
        except TelegramAPIError:
            return
        self._task = asyncio.create_task(self._animate(self._placeholder, laps))

    async def stopped_within(self, seconds: float) -> bool:
        """Wait up to `seconds`; True as soon as `stop` was called."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)
        return self._stopping.is_set()

    async def _animate(self, placeholder: Message, laps: list[str]) -> None:
        index = 0
        delay = self._interval
        while not await self.stopped_within(delay):
            index = (index + 1) % len(laps)
            delay = self._interval
            try:
                await placeholder.edit_text(laps[index])
            except TelegramRetryAfter as error:
                delay = error.retry_after
            except TelegramAPIError:
                return

    async def stop(self) -> None:
        """Stop the animation without cutting an edit short: an edit already sent to
        Telegram finishes first, so it can't land after (and over) the reply. Cancelled only
        if it hangs past `STOP_TIMEOUT`."""
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        await asyncio.wait({task}, timeout=STOP_TIMEOUT)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def finish(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Stop the animation and write `text` into the placeholder. If it can't be edited,
        `text` goes out as a new message and the placeholder is deleted; if that fails too,
        the error is raised and `close` still deletes the placeholder."""
        await self.stop()
        placeholder = self._placeholder
        if placeholder is not None:
            try:
                await with_retries(lambda: placeholder.edit_text(text, reply_markup=reply_markup))
            except TelegramRetryAfter:
                raise
            except TelegramAPIError:
                pass
            else:
                self._finished = True
                return
        await with_retries(lambda: self._message.answer(text, reply_markup=reply_markup))
        await self._delete_placeholder()

    async def close(self) -> None:
        """Stop the animation; a placeholder that never got a reply is deleted."""
        await self.stop()
        if not self._finished:
            await self._delete_placeholder()

    async def _delete_placeholder(self) -> None:
        placeholder, self._placeholder = self._placeholder, None
        self._finished = True
        if placeholder is not None:
            with contextlib.suppress(TelegramAPIError):
                await with_retries(placeholder.delete)


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
