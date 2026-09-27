"""Shared fixtures for bot tests (M5): a real migrated temp DB, and a `Bot` wired to a
`FakeSession` that never touches the network — every outgoing Telegram method is recorded and
answered with a minimal, plausible response built from the request itself.

Updates are fed directly through `dispatcher.feed_update(bot, update)`, exactly the entry
point aiogram's own long-polling loop uses, so these tests exercise the real middleware and
handler chain end to end.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from fitme.bot.app import build_dispatcher
from fitme.config.settings import Settings
from fitme.db.connection import Database, open_database
from fitme.db.migrate import migrate

TEST_TOKEN = "123456:TEST-token-for-unit-tests-only"


class FakeSession(BaseSession):
    """Records every method sent through it; answers each with a minimal response shaped by
    `method.__returning__` (`bool` -> `True`, anything else -> a `Message`)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []
        self._next_message_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError  # pragma: no cover - unused by these tests
        yield b""  # pragma: no cover

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        self.sent.append(method)
        if method.__returning__ is bool:
            return True  # type: ignore[return-value]

        chat_id = getattr(method, "chat_id", None)
        message_id = getattr(method, "message_id", None)
        if message_id is None:
            message_id = self._next_message_id
            self._next_message_id += 1
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        message = Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id if isinstance(chat_id, int) else 0, type="private"),
            text=text,
        )
        return message  # type: ignore[return-value]

    def last_sent_text(self) -> str | None:
        for method in reversed(self.sent):
            text = getattr(method, "text", None) or getattr(method, "caption", None)
            if text is not None:
                return str(text)
        return None


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await open_database(tmp_path / "fitme-bot-test.db")
    await migrate(database)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token=TEST_TOKEN,
        db_path=Path("unused-in-bot-tests.db"),
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
    )


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def bot(session: FakeSession) -> Bot:
    return Bot(token=TEST_TOKEN, session=session)


@pytest.fixture
def dispatcher(db: Database, settings: Settings) -> Dispatcher:
    return build_dispatcher(db, settings)


_next_update_id = 1


def next_update_id() -> int:
    global _next_update_id
    _next_update_id += 1
    return _next_update_id


def make_user(user_id: int, *, language_code: str | None = "en") -> User:
    return User(id=user_id, is_bot=False, first_name="Owner", language_code=language_code)


def message_update(
    *, user: User, chat_id: int, text: str, chat_type: str = "private", caption: str | None = None
) -> Update:
    message = Message(
        message_id=next_update_id(),
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=user,
        text=None if caption is not None else text,
        caption=caption,
    )
    return Update(update_id=next_update_id(), message=message)


def edited_message_update(
    *, user: User, chat_id: int, text: str, chat_type: str = "private"
) -> Update:
    message = Message(
        message_id=next_update_id(),
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=user,
        text=text,
    )
    return Update(update_id=next_update_id(), edited_message=message)


async def all_table_row_counts(db: Database) -> dict[str, int]:
    """Every application table's row count, keyed by table name — used to assert that an
    unknown sender left the database completely untouched (AGENTS.md §5, A§6.1)."""
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ) as cursor,
    ):
        tables = [row[0] for row in await cursor.fetchall()]
    counts: dict[str, int] = {}
    async with db.read() as conn:
        for table in tables:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:  # test-only
                row = await cursor.fetchone()
                assert row is not None
                counts[table] = int(row[0])
    return counts


def callback_update(
    *,
    user: User,
    chat_id: int,
    data: CallbackData | str,
    message_id: int = 1,
    chat_type: str = "private",
) -> Update:
    packed = data.pack() if isinstance(data, CallbackData) else data
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=user,
    )
    callback = CallbackQuery(
        id=str(next_update_id()),
        from_user=user,
        chat_instance="test-chat-instance",
        message=message,
        data=packed,
    )
    return Update(update_id=next_update_id(), callback_query=callback)
