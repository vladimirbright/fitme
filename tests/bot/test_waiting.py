"""The waiting placeholder (`bot/waiting.py`): it animates by editing one message, the reply
is written into it, it is deleted when there is no reply, a 429 is waited out rather than
retried at once, and any other Telegram error stops the animation without losing the reply.
The reply itself waits out a 429; sent as a new message, it takes the placeholder's place; and
a frame edit already in flight can't land over it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message
from conftest import TEST_TOKEN, FakeSession

from fitme.bot import waiting
from fitme.bot.waiting import WaitingMessage, frames, waiting_message

CHAT_ID = 1
INTERVAL = 0.01
SLOW = 60.0  # no frame within a test: only the reply edits the placeholder


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


class FailingAnswerSession(FlakySession):
    """`FlakySession` whose new messages fail once `fail_answers` is set."""

    fail_answers = False

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, SendMessage) and self.fail_answers:
            self.sent.append(method)
            raise _bad_request()
        return await super().make_request(bot, method, timeout)


class GatedSession(FakeSession):
    """Holds every frame edit until `gate` is set (an edit Telegram already received);
    `completed` lists the edits in the order they finished."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.frame_in_flight = asyncio.Event()
        self.completed: list[str] = []

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, EditMessageText) and method.text != "the reply":
            self.frame_in_flight.set()
            await self.gate.wait()
        result = await super().make_request(bot, method, timeout)
        if isinstance(method, EditMessageText):
            self.completed.append(method.text)
        return result


def _retry_after(seconds: int) -> TelegramRetryAfter:
    edit = EditMessageText(text="x", chat_id=CHAT_ID, message_id=1)
    return TelegramRetryAfter(edit, "Too Many Requests", retry_after=seconds)


def _bad_request() -> TelegramBadRequest:
    edit = EditMessageText(text="x", chat_id=CHAT_ID, message_id=1)
    return TelegramBadRequest(edit, "message to edit not found")


def _incoming(bot: Bot) -> Message:
    return Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=CHAT_ID, type="private"), text="hi"
    ).as_(bot)


def _edits(session: FakeSession) -> list[EditMessageText]:
    return [m for m in session.sent if isinstance(m, EditMessageText)]


@pytest.fixture
def paused(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records the 429 waits of a reply's delivery instead of sleeping them."""
    seconds: list[float] = []

    async def record(delay: float) -> None:
        seconds.append(delay)

    monkeypatch.setattr(waiting, "pause", record)
    return seconds


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
    session = FlakySession([_retry_after(7)])
    bot = Bot(token=TEST_TOKEN, session=session)
    waits: list[float] = []
    real_stopped_within = WaitingMessage.stopped_within

    async def recorded(self: WaitingMessage, seconds: float) -> bool:
        waits.append(seconds)
        return await real_stopped_within(self, 0)

    monkeypatch.setattr(WaitingMessage, "stopped_within", recorded)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        while len(_edits(session)) < 3:
            await asyncio.sleep(0)
        await wait.finish("the reply")

    assert waits[:3] == [INTERVAL, 7, INTERVAL]
    assert session.chat_texts() == ["the reply"]


async def test_a_429_on_the_reply_is_waited_out_and_the_reply_still_lands_in_the_placeholder(
    paused: list[float],
) -> None:
    session = FlakySession([_retry_after(3), _retry_after(5)])
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=SLOW) as wait:
        await wait.finish("the reply")

    assert paused == [3, 5]
    assert [m.text for m in _edits(session)] == ["the reply"] * 3
    assert [m for m in session.sent if isinstance(m, SendMessage)] == session.sent[:1]
    assert session.chat_texts() == ["the reply"]


async def test_a_reply_still_rate_limited_after_the_retries_is_raised_and_cleaned_up(
    paused: list[float],
) -> None:
    errors = [_retry_after(1) for _ in range(waiting.DELIVERY_ATTEMPTS)]
    session = FlakySession(errors)
    bot = Bot(token=TEST_TOKEN, session=session)
    with pytest.raises(TelegramRetryAfter):
        async with waiting_message(_incoming(bot), interval=SLOW) as wait:
            await wait.finish("the reply")

    assert len(paused) == waiting.DELIVERY_ATTEMPTS - 1
    assert len(_edits(session)) == waiting.DELIVERY_ATTEMPTS
    assert isinstance(session.sent[-1], DeleteMessage)
    assert session.chat_texts() == []


async def test_a_reply_sent_as_a_new_message_removes_the_placeholder() -> None:
    session = FlakySession([_bad_request()])
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=SLOW) as wait:
        await wait.finish("the reply")

    sends = [m for m in session.sent if isinstance(m, SendMessage)]
    assert [m.text for m in sends][1:] == ["the reply"]
    assert isinstance(session.sent[-1], DeleteMessage)
    assert session.chat_texts() == ["the reply"]


async def test_when_no_reply_can_be_sent_the_placeholder_is_still_removed() -> None:
    session = FailingAnswerSession([_bad_request()])
    bot = Bot(token=TEST_TOKEN, session=session)
    with pytest.raises(TelegramBadRequest):
        async with waiting_message(_incoming(bot), interval=SLOW) as wait:
            session.fail_answers = True
            await wait.finish("the reply")

    assert isinstance(session.sent[-1], DeleteMessage)
    assert session.chat_texts() == []


async def test_a_frame_edit_in_flight_completes_before_the_reply_is_written() -> None:
    session = GatedSession()
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await session.frame_in_flight.wait()
        finishing = asyncio.create_task(wait.finish("the reply"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not finishing.done()
        assert session.completed == []
        assert not any(getattr(m, "text", None) == "the reply" for m in session.sent)

        session.gate.set()
        await finishing

    assert session.completed == [frames("🏃")[1], "the reply"]
    assert session.chat_texts() == ["the reply"]


async def test_a_frame_edit_hanging_past_the_stop_timeout_leaves_the_placeholder_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(waiting, "STOP_TIMEOUT", 0.01)
    session = GatedSession()  # the gate is never opened: the frame edit hangs
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await session.frame_in_flight.wait()
        await wait.finish("the reply")

    assert session.completed == []
    sends = [m for m in session.sent if isinstance(m, SendMessage)]
    assert sends[-1].text == "the reply"
    assert isinstance(session.sent[-1], DeleteMessage)
    assert session.chat_texts() == ["the reply"]


async def test_another_error_stops_the_animation_and_the_reply_still_arrives() -> None:
    session = FlakySession([_bad_request()])
    bot = Bot(token=TEST_TOKEN, session=session)
    async with waiting_message(_incoming(bot), interval=INTERVAL) as wait:
        await asyncio.sleep(INTERVAL * 5)
        assert len(_edits(session)) == 1
        session.errors.append(_bad_request())
        await wait.finish("the reply")

    sends = [m for m in session.sent if isinstance(m, SendMessage)]
    assert sends[-1].text == "the reply"
    assert session.chat_texts() == ["the reply"]
