"""The halt path (A§6.6) and hold clearing: a stop word in free text creates a hold and sends
the fixed halt message with no LLM call; clearing is logged as `hold_clear`; a hold can't be
cleared the same day, but can the next day; and the "same day" check uses the timezone that
was in effect *when the hold was created*, not whatever the user's timezone setting is later
changed to (A§6.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from conftest import FakeSession, callback_update, edited_message_update, make_user, message_update

from fitme import clock
from fitme.bot.callback_data import HoldClear
from fitme.db.connection import Database
from fitme.db.selectors.chat import list_chat_messages_for_user
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.training import list_open_health_holds
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.services import profile as profile_service
from fitme.services import safety
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


async def test_stop_word_in_free_text_halts_with_no_llm_call(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        message_update(user=owner, chat_id=OWNER_CHAT_ID, text="I have chest pain during the set"),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1
    assert holds[0].reason == "stop_word"

    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
    assert any("chest pain" in m.text for m in messages)

    text = session.last_sent_text()
    assert text is not None
    assert "doctor" in text.lower() or "врач" in text.lower()

    # No LLM call is possible here (M5 has no llm/ import in the bot at all); the only
    # observable proxy available at this milestone is that no `llm_calls` row exists.
    async with db.read() as conn, conn.execute("SELECT COUNT(*) FROM llm_calls") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def _halt_via_stop_word(dispatcher: Dispatcher, bot: Bot, db: Database, user_id: int) -> int:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="chest pain, dizzy too")
    )
    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1
    return holds[0].id


async def test_hold_cannot_be_cleared_the_same_day(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    hold_id = await _halt_via_stop_word(dispatcher, bot, db, user_id)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=HoldClear(hold_id=hold_id)),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1  # still open


async def test_hold_can_be_cleared_the_next_day_and_is_logged_as_hold_clear(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    hold_id = await _halt_via_stop_word(dispatcher, bot, db, user_id)
    # Backdate the hold by exactly a day (UTC has no DST, so this always crosses one
    # calendar date, regardless of what the real wall-clock time is when this test runs).
    # The user has no timezone set yet, so `services.safety.can_clear` falls back to UTC.
    yesterday = clock.format_timestamp(clock.now() - timedelta(days=1))
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE health_holds SET created_at = ? WHERE id = ?", (yesterday, hold_id)
        )
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=HoldClear(hold_id=hold_id)),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert holds == []
    hold_clear_decisions = [d for d in decisions if d.kind == "hold_clear"]
    assert len(hold_clear_decisions) == 1
    assert hold_clear_decisions[0].user_report == {"hold_id": hold_id}


async def test_hold_clear_uses_the_timezone_in_effect_when_halted(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await profile_service.set_timezone(db, user_id, "UTC")

    # 23:00 UTC on Jan 10: in UTC+12 (Etc/GMT-12), the same instant is already 2026-01-11.
    fixed_now = datetime(2026, 1, 10, 23, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: fixed_now)

    hold_id = await _halt_via_stop_word(dispatcher, bot, db, user_id)
    async with db.read() as conn:
        (hold,) = await list_open_health_holds(conn, user_id)
    assert hold.id == hold_id

    # Switching the *current* timezone setting to UTC+12 must not, by itself, make the hold
    # look like it was created "yesterday": `can_clear` reads back the timezone recorded at
    # halt time (UTC), not the user's now-changed setting.
    await profile_service.set_timezone(db, user_id, "Etc/GMT-12")
    assert await safety.can_clear(db, hold) is False

    # Less than 12h later, still refused even though it's already "the next calendar day" in
    # the (new, irrelevant) UTC+12 setting.
    monkeypatch.setattr(clock, "now", lambda: fixed_now + timedelta(hours=6))
    assert await safety.can_clear(db, hold) is False

    # >=12h later AND the next calendar day in the *original* timezone (UTC): allowed.
    monkeypatch.setattr(clock, "now", lambda: fixed_now + timedelta(hours=13))
    assert await safety.can_clear(db, hold) is True


async def test_a_stop_word_in_a_photo_caption_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        message_update(
            user=owner,
            chat_id=OWNER_CHAT_ID,
            text="",
            caption="progress pic, but I have chest pain",
        ),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1


async def test_a_stop_word_in_an_edited_message_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        edited_message_update(user=owner, chat_id=OWNER_CHAT_ID, text="actually, chest pain"),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1
    text = session.last_sent_text()
    assert text is not None
    assert "doctor" in text.lower() or "врач" in text.lower()


async def test_activate_arguments_are_never_recorded_in_chat_messages(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    """A§4.1: activation codes are never stored in plaintext — not even when the *bound*
    owner re-runs `/activate` (e.g. out of habit) and it's refused as already-bound."""
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/activate SOME-SECRET-CODE")
    )

    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
    assert all("SOME-SECRET-CODE" not in m.text for m in messages)


async def test_a_stop_word_in_a_commands_arguments_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot,
        message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/start my chest hurts"),
    )

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1
    text = session.last_sent_text()
    assert text is not None
    assert "doctor" in text.lower() or "врач" in text.lower()
