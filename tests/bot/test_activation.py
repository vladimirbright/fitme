"""`/activate <code>` (A§6.1): binding, refusal once bound, expired/reused/wrong codes and the
failed-attempt limit, using the real code-issuing service directly (no CLI subprocess)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher
from conftest import FakeSession, make_user, message_update

from fitme import clock
from fitme.db.connection import Database
from fitme.db.controllers.auth import insert_activation_code
from fitme.db.selectors.auth import get_activation_failed_attempts
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id, get_the_user
from fitme.services.identity import MAX_FAILED_ATTEMPTS, _hash_code, issue_activation_code


async def _the_user(db: Database):  # noqa: ANN202 - test helper, inferred return is fine
    async with db.read() as conn:
        return await get_the_user(conn)


async def _telegram_account(db: Database, telegram_user_id: int):  # noqa: ANN202
    async with db.read() as conn:
        return await get_telegram_account_by_telegram_user_id(conn, telegram_user_id)


async def test_activation_binds_the_account(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(42, language_code="en")

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=42, text=f"/activate {code}")
    )

    user = await _the_user(db)
    assert user is not None
    account = await _telegram_account(db, telegram_user_id=42)
    assert account is not None
    assert account.user_id == user.id
    assert "private Fitme instance" not in (session.last_sent_text() or "")


async def test_a_second_activate_is_refused(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(42)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=42, text=f"/activate {code}")
    )

    second_code = await issue_activation_code(db, rebind=False)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=42, text=f"/activate {second_code}")
    )

    text = session.last_sent_text()
    assert text is not None
    assert "already" in text.lower() or "уже" in text.lower()


async def test_wrong_code_is_rejected(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await issue_activation_code(db, rebind=False)
    owner = make_user(7)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=7, text="/activate NOTTHERIGHTCODE")
    )

    assert await _telegram_account(db, telegram_user_id=7) is None
    text = session.last_sent_text()
    assert text is not None


async def test_expired_code_is_rejected(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    expired_at = clock.now() - timedelta(minutes=1)
    async with db.transaction() as conn:
        await insert_activation_code(
            conn, code_hash=_hash_code("EXPIREDCODE"), expires_at=expired_at
        )
    owner = make_user(8)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=8, text="/activate EXPIREDCODE")
    )

    assert await _telegram_account(db, telegram_user_id=8) is None


async def test_a_used_code_cannot_be_reused(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    first_owner = make_user(1)
    await dispatcher.feed_update(
        bot, message_update(user=first_owner, chat_id=1, text=f"/activate {code}")
    )
    assert await _telegram_account(db, telegram_user_id=1) is not None

    # A stranger reusing the same (now-consumed) code once an account is already bound: the
    # owner gate itself refuses (no account is bound only holds before the first bind), so
    # this can never reach a second bind either way.
    second_owner = make_user(2)
    await dispatcher.feed_update(
        bot, message_update(user=second_owner, chat_id=2, text=f"/activate {code}")
    )

    assert await _telegram_account(db, telegram_user_id=2) is None


async def test_too_many_wrong_codes_invalidates_pending_codes(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(3)
    real_now = clock.now()

    for i in range(MAX_FAILED_ATTEMPTS):
        # Each attempt is >10s apart (the per-chat `/activate` rate limit's window), so the
        # gate lets every one of them reach the handler: this test is about the wrong-code
        # attempt counter, not the rate limiter (covered separately, below).
        monkeypatch.setattr(clock, "now", lambda i=i: real_now + timedelta(seconds=11 * (i + 1)))
        await dispatcher.feed_update(
            bot, message_update(user=owner, chat_id=3, text="/activate WRONGCODEWRONG")
        )

    text = session.last_sent_text()
    assert text is not None
    assert "too many" in text.lower() or "слишком" in text.lower()

    # The originally-valid code is now invalidated too (A§6.1: the pending codes are
    # invalidated, not just the wrong guesses). Space this attempt out past the rate limit
    # too, so it actually reaches the handler and exercises the "invalid code" path.
    monkeypatch.setattr(
        clock, "now", lambda: real_now + timedelta(seconds=11 * (MAX_FAILED_ATTEMPTS + 1))
    )
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=3, text=f"/activate {code}")
    )
    assert await _telegram_account(db, telegram_user_id=3) is None


async def test_activate_attempts_are_rate_limited_per_chat(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await issue_activation_code(db, rebind=False)
    owner = make_user(9)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=9, text="/activate WRONGONE")
    )
    attempts_after_first = await _failed_attempts(db)
    assert attempts_after_first == 1

    # A second attempt from the same chat, immediately after: the gate's per-chat rate limit
    # (A§6.1) drops it before it ever reaches the handler — no counter increment — but it
    # does get a single "please wait" reply rather than silence.
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=9, text="/activate WRONGTWO")
    )
    assert await _failed_attempts(db) == attempts_after_first  # not incremented
    text = session.last_sent_text()
    assert text is not None
    assert "wait" in text.lower() or "подожд" in text.lower()

    # A third attempt, still within the same (10-minute) reply window: the "please wait"
    # notice itself is throttled too, so this one is once again completely silent.
    sent_before = len(session.sent)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=9, text="/activate WRONGTHREE")
    )
    assert len(session.sent) == sent_before
    assert await _failed_attempts(db) == attempts_after_first


async def _failed_attempts(db: Database) -> int:
    async with db.read() as conn:
        return await get_activation_failed_attempts(conn)


async def test_activate_without_a_code_shows_usage(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    owner = make_user(5)

    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=5, text="/activate"))

    text = session.last_sent_text()
    assert text is not None
    assert "/activate" in text
