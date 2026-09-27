"""The setup questionnaire (A§5.1, M5 accept list): a full run stores canonical enum values,
a red flag without clearance blocks `guards.screening.plan_allowed`, Back navigation works,
stale callbacks are ignored, and a restart mid-setup resumes at the same step."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from conftest import FakeSession, callback_update, make_user, message_update

from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import SetupChoice, SetupNav, SetupToggle
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.selectors.profile import (
    get_profile,
    get_screening_flag,
    get_setup_progress,
    list_screening_flags,
    list_screening_notes,
)
from fitme.db.selectors.training import list_open_health_holds
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import RED_FLAGS, ScreeningFlag
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.screening import plan_allowed
from fitme.services import profile as profile_service
from fitme.services.identity import issue_activation_code

OWNER_CHAT_ID = 1


async def _activate(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    """Runs `/activate` and returns the bound user's internal id."""
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None
    return account.user_id


async def _click(dispatcher: Dispatcher, bot: Bot, data: object) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot,
        callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=data),  # type: ignore[arg-type]
    )


async def _send_text(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=text))


async def _run_full_setup_with_no_red_flags(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    user_id = await _activate(dispatcher, bot, db)

    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))
    await _click(dispatcher, bot, SetupChoice(step="age", value="30_39"))
    await _click(dispatcher, bot, SetupChoice(step="weight", value="70_79"))
    await _click(dispatcher, bot, SetupChoice(step="experience_duration", value="6m_2y"))
    await _click(dispatcher, bot, SetupChoice(step="experience_barbell", value="some"))
    for flag in sorted(RED_FLAGS, key=lambda f: f.value):
        await _click(dispatcher, bot, SetupChoice(step=f"screening_{flag.value}", value="no"))
    await _click(dispatcher, bot, SetupToggle(step="screening_areas", value="knee_injury_current"))
    await _click(dispatcher, bot, SetupNav(step="screening_areas", action="next"))
    await _click(dispatcher, bot, SetupNav(step="screening_other", action="next"))
    # No red flag and no other_unlisted was set "yes": screening_clearance is skipped.
    await _click(dispatcher, bot, SetupToggle(step="preferences", value="full_body"))
    await _click(dispatcher, bot, SetupNav(step="preferences", action="next"))
    await _click(dispatcher, bot, SetupChoice(step="focus", value="strength"))
    await _click(dispatcher, bot, SetupChoice(step="location", value="home_equipment"))
    await _click(dispatcher, bot, SetupToggle(step="equipment", value="dumbbells"))
    await _click(dispatcher, bot, SetupNav(step="equipment", action="next"))
    await _click(dispatcher, bot, SetupChoice(step="frequency", value="3"))
    await _click(dispatcher, bot, SetupChoice(step="session_length", value="45"))
    await _click(dispatcher, bot, SetupNav(step="summary", action="next"))
    return user_id


async def test_full_setup_run_stores_canonical_enum_values(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _run_full_setup_with_no_red_flags(dispatcher, bot, db)

    async with db.read() as conn:
        profile = await get_profile(conn, user_id)
        flags = await list_screening_flags(conn, user_id)
        progress = await get_setup_progress(conn, user_id)

    assert profile is not None
    assert profile.age_bucket == "30_39"
    assert profile.weight_bucket == "70_79"
    assert profile.experience == "6m_2y"
    assert profile.barbell_experience == "some"
    assert profile.preferences == ["full_body"]
    assert profile.focus == "strength"
    assert profile.location == "home_equipment"
    assert profile.equipment == ["dumbbells"]
    assert profile.sessions_per_week == 3
    assert profile.session_minutes == 45
    assert profile.completed_at is not None

    by_flag = {f.flag: f for f in flags}
    for red_flag in RED_FLAGS:
        assert by_flag[red_flag.value].value == "no"
    assert by_flag["knee_injury_current"].value == "yes"
    assert by_flag["shoulder_injury_current"].value == "no"
    assert by_flag["other_unlisted"].value == "no"

    # Setup is complete: no dangling progress marker left to "resume".
    assert progress is None


async def test_red_flag_without_clearance_blocks_plan_allowed(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)

    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))
    await _click(dispatcher, bot, SetupChoice(step="age", value="30_39"))
    await _click(dispatcher, bot, SetupChoice(step="weight", value="70_79"))
    await _click(dispatcher, bot, SetupChoice(step="experience_duration", value="none"))
    await _click(dispatcher, bot, SetupChoice(step="experience_barbell", value="no"))
    red_flags_in_order = sorted(RED_FLAGS, key=lambda f: f.value)
    await _click(
        dispatcher, bot, SetupChoice(step=f"screening_{red_flags_in_order[0].value}", value="yes")
    )
    for flag in red_flags_in_order[1:]:
        await _click(dispatcher, bot, SetupChoice(step=f"screening_{flag.value}", value="no"))
    await _click(dispatcher, bot, SetupNav(step="screening_areas", action="next"))
    await _click(dispatcher, bot, SetupNav(step="screening_other", action="next"))
    # A red flag was "yes": the clearance step must now be shown. Answer "no".
    await _click(dispatcher, bot, SetupChoice(step="screening_clearance", value="no"))

    async with db.read() as conn:
        flags = await list_screening_flags(conn, user_id)
    states = [
        ScreeningFlagState(flag=ScreeningFlag(f.flag), value=f.value, clearance=f.clearance)
        for f in flags
    ]
    verdict = plan_allowed(states, holds=[])
    assert verdict.ok is False
    assert "clearance" in verdict.detail


async def test_back_navigation_returns_to_the_previous_step(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))
    await _click(dispatcher, bot, SetupChoice(step="age", value="30_39"))

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None
    assert progress.step == "weight"

    await _click(dispatcher, bot, SetupNav(step="weight", action="back"))

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None
    assert progress.step == "age"


async def test_stale_setup_callback_is_ignored(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    # Now on "timezone". Send a stale callback claiming to answer "age" (a step already left).
    await _click(dispatcher, bot, SetupChoice(step="age", value="18_29"))

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
        profile = await get_profile(conn, user_id)
    assert progress is not None
    assert progress.step == "timezone"  # unchanged: the stale callback had no effect
    assert profile is None or profile.age_bucket is None


async def test_setup_resumes_after_a_restart(
    settings: Settings, bot: Bot, session: FakeSession, db: Database
) -> None:
    dispatcher = build_dispatcher(db, settings)
    user_id = await _activate(dispatcher, bot, db)
    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))

    # Simulate a process restart: build a brand-new Dispatcher over the same (still open)
    # database. No in-memory FSM state survives this; only what's in the database does.
    restarted = build_dispatcher(db, settings)
    await _click(restarted, bot, SetupChoice(step="age", value="40_49"))

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
        profile = await get_profile(conn, user_id)
    assert progress is not None
    assert progress.step == "weight"
    assert profile is not None
    assert profile.age_bucket == "40_49"


async def test_a_halting_screening_other_note_is_still_persisted(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    """B2 (A§6.6): the note is saved (and `other_unlisted` set to "yes", needing clearance)
    *before* the stop-word scan runs, so a halting note is never lost."""
    user_id = await _activate(dispatcher, bot, db)
    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))
    await _click(dispatcher, bot, SetupChoice(step="age", value="30_39"))
    await _click(dispatcher, bot, SetupChoice(step="weight", value="70_79"))
    await _click(dispatcher, bot, SetupChoice(step="experience_duration", value="none"))
    await _click(dispatcher, bot, SetupChoice(step="experience_barbell", value="no"))
    for flag in sorted(RED_FLAGS, key=lambda f: f.value):
        await _click(dispatcher, bot, SetupChoice(step=f"screening_{flag.value}", value="no"))
    await _click(dispatcher, bot, SetupNav(step="screening_areas", action="next"))

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None
    assert progress.step == "screening_other"

    await _send_text(dispatcher, bot, "I have chest pain and it won't stop")

    async with db.read() as conn:
        flags = await list_screening_flags(conn, user_id)
        notes = await list_screening_notes(conn, user_id)
        progress = await get_setup_progress(conn, user_id)
        holds = await list_open_health_holds(conn, user_id)
    by_flag = {f.flag: f for f in flags}
    assert by_flag["other_unlisted"].value == "yes"
    assert any("chest pain" in note.text for note in notes)
    assert len(holds) == 1
    # B2: the note is the definitive answer regardless of halting, so the step still
    # advances past screening_other — here to screening_clearance, since `other_unlisted`
    # now needs clearance. No stale Skip button for screening_other can remain live.
    assert progress is not None
    assert progress.step == "screening_clearance"

    # And guards.screening.plan_allowed refuses without clearance (M5 accept list).
    async with db.read() as conn:
        all_flags = await list_screening_flags(conn, user_id)
    states = [
        ScreeningFlagState(flag=ScreeningFlag(f.flag), value=f.value, clearance=f.clearance)
        for f in all_flags
    ]
    verdict = plan_allowed(states, holds=[])
    assert verdict.ok is False


async def test_a_stale_skip_callback_for_screening_other_is_rejected(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    """A Skip button rendered *before* a note was submitted, if pressed afterwards, is
    rejected as stale (`setup_progress.step` has already moved on) rather than reopening
    `screening_other` or touching `other_unlisted` (B2)."""
    user_id = await _activate(dispatcher, bot, db)
    await _click(dispatcher, bot, SetupChoice(step="language", value="en"))
    await _click(dispatcher, bot, SetupChoice(step="timezone", value="UTC"))
    await _click(dispatcher, bot, SetupChoice(step="age", value="30_39"))
    await _click(dispatcher, bot, SetupChoice(step="weight", value="70_79"))
    await _click(dispatcher, bot, SetupChoice(step="experience_duration", value="none"))
    await _click(dispatcher, bot, SetupChoice(step="experience_barbell", value="no"))
    for flag in sorted(RED_FLAGS, key=lambda f: f.value):
        await _click(dispatcher, bot, SetupChoice(step=f"screening_{flag.value}", value="no"))
    await _click(dispatcher, bot, SetupNav(step="screening_areas", action="next"))
    await _send_text(dispatcher, bot, "shoulder has been sore for weeks")

    async with db.read() as conn:
        progress = await get_setup_progress(conn, user_id)
    assert progress is not None
    assert progress.step != "screening_other"  # already advanced past it

    # The (now-stale) Skip button from the screening_other prompt is pressed anyway.
    await _click(dispatcher, bot, SetupNav(step="screening_other", action="next"))

    async with db.read() as conn:
        flag = await get_screening_flag(conn, user_id, "other_unlisted")
        progress = await get_setup_progress(conn, user_id)
    assert flag is not None
    assert flag.value == "yes"  # untouched by the stale Skip
    assert progress is not None
    assert progress.step != "screening_other"  # navigation did not move backwards either


async def test_skip_never_downgrades_yes_but_a_profile_reset_allows_it(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    """B2 belt-and-braces: `skip_other_note` never downgrades an existing "yes" (defends
    against a stale or racing Skip undoing a note); `reset_other_unlisted` (used by a
    deliberate `/profile` re-run) gives Skip a clean slate to legitimately set "no"."""
    user_id = await _activate(dispatcher, bot, db)

    await profile_service.submit_other_note(db, user_id, "old shoulder issue")
    async with db.read() as conn:
        flag = await get_screening_flag(conn, user_id, "other_unlisted")
    assert flag is not None
    assert flag.value == "yes"

    await profile_service.skip_other_note(db, user_id)
    async with db.read() as conn:
        flag = await get_screening_flag(conn, user_id, "other_unlisted")
    assert flag is not None
    assert flag.value == "yes"  # not downgraded

    await profile_service.reset_other_unlisted(db, user_id)
    await profile_service.skip_other_note(db, user_id)
    async with db.read() as conn:
        flag = await get_screening_flag(conn, user_id, "other_unlisted")
    assert flag is not None
    assert flag.value == "no"  # a deliberate reset does allow the change
