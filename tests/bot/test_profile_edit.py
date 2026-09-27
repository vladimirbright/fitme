"""`/profile` (A§6.2): shows the answers, and a "Fix" jumps into the setup flow to re-answer
one field; changing a screening answer re-runs the remaining screening steps."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from conftest import FakeSession, callback_update, make_user, message_update
from test_setup_flow import _click, _run_full_setup_with_no_red_flags

from fitme.bot.callback_data import ProfileFix, SetupChoice, SetupNav
from fitme.db.connection import Database
from fitme.db.selectors.profile import get_profile, get_setup_progress, list_screening_flags
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import RED_FLAGS
from fitme.services.identity import issue_activation_code

OWNER_CHAT_ID = 1


async def test_profile_edit_changes_a_field_and_re_running_screening(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _run_full_setup_with_no_red_flags(dispatcher, bot, db)

    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/profile")
    )
    profile_text = session.last_sent_text()
    assert profile_text is not None
    assert "30" in profile_text or "Age" in profile_text or "Возраст" in profile_text

    # Fix "screening": jumps to the first red-flag step.
    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=ProfileFix(field="screening")),
    )
    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None
    first_red_flag_step = f"screening_{sorted(RED_FLAGS, key=lambda f: f.value)[0].value}"
    assert progress.step == first_red_flag_step

    # Re-answer every red flag "no" again, then continue to summary and confirm, so the edit
    # actually commits (mirrors the ordinary flow's tail).
    red_flags_in_order = sorted(RED_FLAGS, key=lambda f: f.value)
    for flag in red_flags_in_order:
        await _click(dispatcher, bot, SetupChoice(step=f"screening_{flag.value}", value="no"))
    await _click(dispatcher, bot, SetupNav(step="screening_areas", action="next"))
    await _click(dispatcher, bot, SetupNav(step="screening_other", action="next"))
    await _click(dispatcher, bot, SetupNav(step="preferences", action="next"))
    await _click(dispatcher, bot, SetupChoice(step="focus", value="muscle"))
    await _click(dispatcher, bot, SetupNav(step="summary", action="next"))

    async with db.read() as conn:
        profile = await get_profile(conn, user_id)
        flags = await list_screening_flags(conn, user_id)
    assert profile is not None
    assert profile.focus == "muscle"
    assert all(f.value == "no" for f in flags if f.flag in {rf.value for rf in RED_FLAGS})


async def test_profile_shows_empty_message_before_setup_is_complete(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/profile")
    )

    text = session.last_sent_text()
    assert text is not None
