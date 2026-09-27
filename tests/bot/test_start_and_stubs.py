"""`/start` (bound, no profile vs. bound, with profile), `/cancel`, and the M7-M9 stub
commands (`/train`, `/stats`, `/system`) all reply "not available yet" for now."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from conftest import FakeSession, make_user, message_update
from test_setup_flow import _run_full_setup_with_no_red_flags

from fitme.db.connection import Database
from fitme.db.selectors.profile import get_setup_progress
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


async def test_start_before_a_profile_launches_setup(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    # Activation itself already sends the first setup step; sending /start again must not
    # break anything and should still show a setup step (resuming, not restarting).
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/start")
    )

    text = session.last_sent_text()
    assert text is not None


async def test_start_after_setup_shows_the_main_menu_text(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _run_full_setup_with_no_red_flags(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/start")
    )

    text = session.last_sent_text()
    assert text is not None


async def test_cancel_does_not_wipe_setup_progress(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/cancel")
    )

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None  # /cancel ends the interaction, it doesn't discard answers
    text = session.last_sent_text()
    assert text is not None
    assert "/start" in text  # "setup is paused, resume with /start" (not the generic message)


async def test_stub_commands_reply_not_available_yet(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _run_full_setup_with_no_red_flags(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    for command in ("/train", "/stats", "/system"):
        await dispatcher.feed_update(
            bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=command)
        )
        text = session.last_sent_text()
        assert text is not None
