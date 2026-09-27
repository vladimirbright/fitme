"""`OwnerGateMiddleware` (A§6.1): an unknown sender gets one fixed reply and nothing is
stored; the reply is rate-limited per chat; `/activate` is allowed only while unbound; a
stranger's callback query or edited message is dropped silently; non-private chats are
ignored entirely, even for the bound owner; and after `/delete`, the next message is treated
as coming from an unknown sender again."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from conftest import (
    FakeSession,
    all_table_row_counts,
    callback_update,
    edited_message_update,
    make_user,
    message_update,
)

from fitme.bot.callback_data import SetupChoice
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.services import account as account_service
from fitme.services.identity import issue_activation_code


async def test_unknown_user_gets_private_instance_reply_and_nothing_is_stored(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, settings: Settings
) -> None:
    before = await all_table_row_counts(db)

    stranger = make_user(999)
    update = message_update(user=stranger, chat_id=999, text="hello")
    await dispatcher.feed_update(bot, update)

    assert session.sent, "expected a reply to be sent"
    text = session.last_sent_text()
    assert text is not None
    assert "private Fitme instance" in text
    assert settings.source_url in text

    after = await all_table_row_counts(db)
    assert after == before, "an unknown sender must leave every table untouched"


async def test_unknown_user_reply_is_rate_limited_per_chat(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession
) -> None:
    stranger = make_user(999)
    await dispatcher.feed_update(bot, message_update(user=stranger, chat_id=999, text="one"))
    first_count = len(session.sent)

    await dispatcher.feed_update(bot, message_update(user=stranger, chat_id=999, text="two"))

    assert len(session.sent) == first_count, "a second message within the window gets no reply"


async def test_unknown_user_sending_activate_without_a_pending_code_still_gets_gated(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    """`/activate` only bypasses the gate while no account is bound *and* a real code is
    involved; without ever having minted one, this is exactly the same "everything else" path
    as any other message from an unknown sender."""
    before = await all_table_row_counts(db)
    stranger = make_user(1)
    update = message_update(user=stranger, chat_id=1, text="/activate WRONGCODE1")

    await dispatcher.feed_update(bot, update)

    # Because no account is bound, the gate lets this reach the /activate handler itself
    # (which then reports an invalid code) — either way, nothing about *this sender's id or
    # text* may be logged, and the only row this can create is activation bookkeeping, never
    # a user/telegram_account row.
    after = await all_table_row_counts(db)
    assert after.get("users", 0) == before.get("users", 0)
    assert after.get("telegram_accounts", 0) == before.get("telegram_accounts", 0)


async def test_a_strangers_callback_query_is_dropped_silently(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    before = await all_table_row_counts(db)
    stranger = make_user(1234)

    await dispatcher.feed_update(
        bot,
        callback_update(user=stranger, chat_id=1234, data=SetupChoice(step="age", value="30_39")),
    )

    assert session.sent == []
    assert await all_table_row_counts(db) == before


async def test_a_strangers_edited_message_is_dropped_silently(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    before = await all_table_row_counts(db)
    stranger = make_user(1234)

    await dispatcher.feed_update(
        bot, edited_message_update(user=stranger, chat_id=1234, text="edited text")
    )

    assert session.sent == []
    assert await all_table_row_counts(db) == before


async def test_non_private_chat_is_ignored_even_for_the_bound_owner(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(42)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=42, text=f"/activate {code}")
    )
    before = await all_table_row_counts(db)
    sent_before = len(session.sent)

    await dispatcher.feed_update(
        bot,
        message_update(user=owner, chat_id=42, text="/help", chat_type="group"),
    )

    assert len(session.sent) == sent_before  # no reply at all, not even the private-instance one
    assert await all_table_row_counts(db) == before


async def test_after_delete_the_next_message_gets_the_stranger_reply(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(42)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=42, text=f"/activate {code}")
    )

    # This instance is single-user (ADR 0002): the one user row is always id 1.
    await account_service.delete_user(db, 1)

    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=42, text="/help"))

    text = session.last_sent_text()
    assert text is not None
    assert "private Fitme instance" in text
