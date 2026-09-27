"""`/export` sends a document; `/delete`'s two-step confirmation (button, then typing
`DELETE`) wipes everything; `/help` contains the AI disclosure."""

from __future__ import annotations

import json

from aiogram import Bot, Dispatcher
from aiogram.methods import SendDocument
from conftest import FakeSession, all_table_row_counts, callback_update, make_user, message_update

from fitme.bot.callback_data import DeleteConfirm
from fitme.db.connection import Database
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.services.identity import issue_activation_code

OWNER_CHAT_ID = 1


async def _activate(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None
    return account.user_id


async def test_help_contains_the_ai_disclosure(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/help")
    )

    text = session.last_sent_text()
    assert text is not None
    assert "AI" in text
    assert "/plan" in text
    assert "/train" in text


async def test_export_sends_a_document(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/export")
    )

    documents = [m for m in session.sent if isinstance(m, SendDocument)]
    assert len(documents) == 1
    payload = documents[0].document.data  # type: ignore[union-attr]
    parsed = json.loads(payload.decode("utf-8"))
    assert len(parsed["users"]) == 1


async def test_delete_two_step_flow_wipes_everything(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/delete")
    )
    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=DeleteConfirm(action="start")),
    )
    # Typing anything other than DELETE must not delete the account.
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="delete")
    )
    counts_before_confirm = await all_table_row_counts(db)
    assert counts_before_confirm.get("users", 0) == 1

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="DELETE")
    )

    counts = await all_table_row_counts(db)
    assert all(count == 0 for count in counts.values()), counts


async def test_delete_can_be_cancelled(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/delete")
    )
    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=DeleteConfirm(action="cancel")),
    )
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="DELETE")
    )

    counts = await all_table_row_counts(db)
    assert counts.get("users", 0) == 1  # the cancelled flow never deletes the account
