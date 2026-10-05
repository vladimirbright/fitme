"""`services.conversations` (ADR 0004): one planning and/or one training session, the
training one winning; a planning session for another plan replaces the old one; an idle
planning session and a training session whose workout ended are closed; history is the
session's own turns; retention removes closed sessions with the chat messages."""

from __future__ import annotations

from datetime import timedelta

from fitme import clock
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan
from fitme.db.controllers.training import finish_workout_session, insert_workout_session
from fitme.db.selectors.conversations import list_open_conversations
from fitme.services import conversations, retention
from fitme.services.safety import record_incoming_text


async def _plan(db: Database, user_id: int, name: str) -> int:
    async with db.transaction() as conn:
        return await insert_plan(
            conn, user_id=user_id, name=name, is_default=False, status="active"
        )


async def _draft(db: Database, user_id: int) -> int:
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_revise",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )


async def _age(db: Database, *, hours: int) -> None:
    old = clock.format_timestamp(clock.now() - timedelta(hours=hours))
    async with db.transaction() as conn:
        await conn.execute("UPDATE conversations SET updated_at = ?, closed_at = ?", (old, old))


async def test_a_planning_session_is_reused_for_its_plan_and_replaced_for_another(
    db: Database, user_id: int
) -> None:
    one, two = await _plan(db, user_id, "One"), await _plan(db, user_id, "Two")
    first = await conversations.open_planning(db, user_id, one)
    assert (await conversations.open_planning(db, user_id, one)).id == first.id
    second = await conversations.open_planning(db, user_id, two)
    assert second.id != first.id
    async with db.read() as conn:
        assert [c.id for c in await list_open_conversations(conn, user_id)] == [second.id]

    draft = await _draft(db, user_id)
    shown = await conversations.planning_draft_shown(
        db, user_id, plan_id=two, draft_decision_id=draft
    )
    assert shown.id == second.id and shown.draft_decision_id == draft
    assert (await conversations.active(db, user_id)) == shown

    closed = await conversations.close_planning(db, user_id, status="saved")
    assert closed is not None and await conversations.active(db, user_id) is None


async def test_an_idle_planning_session_ends(db: Database, user_id: int) -> None:
    await conversations.open_planning(db, user_id, None)
    await _age(db, hours=conversations.PLANNING_IDLE_HOURS + 1)
    assert await conversations.active(db, user_id) is None
    async with db.read() as conn:
        assert await list_open_conversations(conn, user_id) == []


async def test_a_training_session_lives_exactly_as_long_as_the_workout(
    db: Database, user_id: int
) -> None:
    await conversations.open_planning(db, user_id, None)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=None, workout_key="A", status="in_progress"
        )
    training = await conversations.active(db, user_id)
    assert training is not None and training.kind == conversations.TRAINING
    assert (await conversations.active(db, user_id)) == training  # one per workout
    # The planning session is still there, just not the active one.
    assert await conversations.current_planning(db, user_id) is not None

    async with db.transaction() as conn:
        await finish_workout_session(conn, session_id, status="completed")
    active = await conversations.active(db, user_id)
    assert active is not None and active.kind == conversations.PLANNING


async def test_history_is_the_sessions_own_turns_without_the_current_message(
    db: Database, user_id: int
) -> None:
    record = await conversations.open_planning(db, user_id, None)
    first = await record_incoming_text(db, user_id=user_id, session_id=None, text="squat 80")
    await conversations.link_to_active(db, user_id, first)
    await conversations.record_outgoing(db, user_id, record.id, "Limited to 62.5.")
    current = await record_incoming_text(db, user_id=user_id, session_id=None, text="ok, save")
    await conversations.link_to_active(db, user_id, current)
    await record_incoming_text(db, user_id=user_id, session_id=None, text="not linked")

    turns = await conversations.history(db, record.id, exclude_message_id=current)
    assert [(turn.role, turn.text) for turn in turns] == [
        ("user", "squat 80"),
        ("assistant", "Limited to 62.5."),
    ]


async def test_retention_removes_closed_sessions_with_their_messages(
    db: Database, user_id: int
) -> None:
    record = await conversations.open_planning(db, user_id, None)
    await conversations.record_outgoing(db, user_id, record.id, "hello")
    await conversations.close_planning(db, user_id, status="discarded")
    await conversations.open_planning(db, user_id, None)  # still open: kept
    await _age(db, hours=24 * 400)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE chat_messages SET created_at = ?",
            (clock.format_timestamp(clock.now() - timedelta(days=400)),),
        )
    await retention.purge(db, chat_retention_days=365)
    async with db.read() as conn, conn.execute("SELECT status FROM conversations") as cursor:
        assert [row[0] for row in await cursor.fetchall()] == ["open"]
