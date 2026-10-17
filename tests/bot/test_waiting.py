"""The waiting placeholder (`bot/waiting.py`): it animates by editing one message, the reply
is written into it, it is deleted when there is no reply, a 429 is waited out rather than
retried at once, and any other Telegram error stops the animation without losing the reply."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message
from conftest import TEST_TOKEN, FakeSession

from fitme.bot import waiting
from fitme.bot.waiting import frames, waiting_message

CHAT_ID = 1
INTERVAL = 0.01


class FlakySession(FakeSession):
    """Raises `errors` (in order) for the first edits, then behaves like `FakeSession`."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = errors

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, EditMessageText) and self.errors:
            self.sent.append(method)
            raise self.errors.pop(0)
        return await super().make_request(bot, method, timeout)


def _incoming(bot: Bot) -> Message:
    return Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=CHAT_ID, type="private"), text="hi"
    ).as_(bot)


def _edits(session: FakeSession) -> list[EditMessageText]:
    return [m for m in session.sent if isinstance(m, EditMessageText)]


@pytest.fixture(autouse=True)
def one_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiting, "RUNNERS", ("🏃",))


def test_a_lap_moves_the_runner_right_to_left_on_a_fixed_track() -> None:
    lap = frames("🏃", length=3)
    assert lap == ["...🏃", "..🏃.", ".🏃..", "🏃..."]


async def test_the_placeholder_animates_and_becomes_the_reply(session: FakeSession) -> None:
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await asyncio.sleep(INTERVAL * 5)
        await wait.finish("the reply")

    first = session.sent[0]
    assert isinstance(first, SendMessage) and first.text == frames("🏃")[0]
    edits = _edits(session)
    assert len(edits) >= 3
    assert [e.text for e in edits[:2]] == frames("🏃")[1:3]
    assert edits[-1].text == "the reply"
    assert not any(isinstance(m, DeleteMessage) for m in session.sent)
    assert session.chat_texts() == ["the reply"]


async def test_no_edit_after_the_reply(session: FakeSession) -> None:
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await wait.finish("the reply")
        sent = len(session.sent)
        await asyncio.sleep(INTERVAL * 5)
    assert len(session.sent) == sent


async def test_without_a_reply_the_placeholder_is_deleted(session: FakeSession) -> None:
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL):
        pass
    assert isinstance(session.sent[-1], DeleteMessage)
    assert session.chat_texts() == []


async def test_the_placeholder_is_deleted_when_the_wait_fails(session: FakeSession) -> None:
    bot = Bot(token=TEST_TOKEN, session=session)
    with pytest.raises(RuntimeError):
        async with waiting_message(_incoming(bot), interval=INTERVAL):
            raise RuntimeError("model call failed")
    assert isinstance(session.sent[-1], DeleteMessage)


async def test_a_429_is_waited_out_before_the_next_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    edit = EditMessageText(text="x", chat_id=CHAT_ID, message_id=1)
    session = FlakySession([TelegramRetryAfter(edit, "Too Many Requests", retry_after=7)])
    bot = Bot(token=TEST_TOKEN, session=session)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *args: Any) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(waiting.asyncio, "sleep", fake_sleep)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        while len(_edits(session)) < 3:
            await real_sleep(0)
        await wait.finish("the reply")

    assert slept[:3] == [INTERVAL, 7, INTERVAL]
    assert session.chat_texts() == ["the reply"]


async def test_another_error_stops_the_animation_and_the_reply_still_arrives() -> None:
    edit = EditMessageText(text="x", chat_id=CHAT_ID, message_id=1)
    session = FlakySession([TelegramBadRequest(edit, "message to edit not found")])
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await asyncio.sleep(INTERVAL * 5)
        assert len(_edits(session)) == 1
        session.errors.append(TelegramBadRequest(edit, "message to edit not found"))
        await wait.finish("the reply")

    last = session.sent[-1]
    assert isinstance(last, SendMessage) and last.text == "the reply"
