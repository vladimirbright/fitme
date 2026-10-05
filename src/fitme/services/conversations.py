"""Conversation sessions of the free-text assistant (ADR 0004).

A session gives the assistant memory for exactly one piece of work, and ends with it:

- **planning** — a new plan, or a change of one existing plan. Every step is a draft round
  (the same `plan_generate`/`plan_revise` decisions `/plan` uses); the session remembers the
  current one (`draft_decision_id`). It ends when the owner saves the draft (`saved`), closes
  it without saving (`discarded`), or leaves it idle for `PLANNING_IDLE_HOURS` (`closed`).
- **training** — a started workout (`in_progress`), from Start until it is completed,
  aborted or halted. Created on the first message during the workout and closed as soon as
  the workout is no longer in progress.

At most one session is active: a training session wins over a planning one, so text during a
workout is about the workout. The text itself stays in `chat_messages` (retention A§8.4);
this module only links messages to their session and reads the last turns back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fitme import clock
from fitme.db.connection import Database
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.controllers.conversations import (
    close_conversation,
    insert_conversation,
    link_chat_message,
    set_conversation_draft,
    touch_conversation,
)
from fitme.db.records import ConversationRecord
from fitme.db.selectors.conversations import (
    get_conversation,
    list_conversation_messages,
    list_open_conversations,
)
from fitme.db.selectors.training import get_active_workout_session, get_workout_session
from fitme.domain.enums import WorkoutSessionStatus

PLANNING = "planning"
TRAINING = "training"
# A planning session nobody touched for this long is over: its draft stays a draft (it can
# still be confirmed from its own buttons), but new text starts fresh.
PLANNING_IDLE_HOURS = 12
HISTORY_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Turn:
    role: str  # "user" | "assistant"
    text: str


def _idle(record: ConversationRecord) -> bool:
    updated = clock.parse_timestamp(record.updated_at)
    return clock.now() - updated > timedelta(hours=PLANNING_IDLE_HOURS)


async def active(db: Database, user_id: int) -> ConversationRecord | None:
    """The session new text belongs to, or `None`. Closes what has ended on the way: a
    training session whose workout is no longer in progress, an idle planning session."""
    async with db.transaction() as conn:
        open_ = await list_open_conversations(conn, user_id)
        workout = await get_active_workout_session(conn, user_id)
        in_progress = (
            workout
            if workout is not None and workout.status == WorkoutSessionStatus.IN_PROGRESS.value
            else None
        )
        training: ConversationRecord | None = None
        planning: ConversationRecord | None = None
        for record in open_:
            if record.kind == TRAINING:
                if in_progress is not None and record.workout_session_id == in_progress.id:
                    training = training or record
                else:
                    await close_conversation(conn, record.id, status="closed")
            elif planning is None and not _idle(record):
                planning = record
            else:
                await close_conversation(conn, record.id, status="closed")
        if in_progress is not None and training is None:
            conversation_id = await insert_conversation(
                conn,
                user_id=user_id,
                kind=TRAINING,
                plan_id=None,
                workout_session_id=in_progress.id,
            )
            training = await get_conversation(conn, conversation_id)
    return training if training is not None else planning


async def open_planning(db: Database, user_id: int, plan_id: int | None) -> ConversationRecord:
    """The open planning session for `plan_id` (`None`: a new plan), reusing the current one
    when it is about the same plan, otherwise closing it and starting a new one."""
    async with db.transaction() as conn:
        for record in await list_open_conversations(conn, user_id):
            if record.kind != PLANNING:
                continue
            if record.plan_id == plan_id and not _idle(record):
                await touch_conversation(conn, record.id)
                refreshed = await get_conversation(conn, record.id)
                assert refreshed is not None
                return refreshed
            await close_conversation(conn, record.id, status="closed")
        conversation_id = await insert_conversation(
            conn, user_id=user_id, kind=PLANNING, plan_id=plan_id, workout_session_id=None
        )
        created = await get_conversation(conn, conversation_id)
    assert created is not None
    return created


async def planning_draft_shown(
    db: Database, user_id: int, *, plan_id: int | None, draft_decision_id: int
) -> ConversationRecord:
    """A draft round was shown (any entry point: a button, the assistant, a pasted
    program): it is now the current draft of the planning session for that plan."""
    record = await open_planning(db, user_id, plan_id)
    async with db.transaction() as conn:
        await set_conversation_draft(conn, record.id, draft_decision_id=draft_decision_id)
        refreshed = await get_conversation(conn, record.id)
    assert refreshed is not None
    return refreshed


async def close_planning(db: Database, user_id: int, *, status: str) -> ConversationRecord | None:
    """End the open planning session, if any (`saved` or `discarded`)."""
    async with db.transaction() as conn:
        for record in await list_open_conversations(conn, user_id):
            if record.kind == PLANNING:
                await close_conversation(conn, record.id, status=status)
                return record
    return None


async def current_planning(db: Database, user_id: int) -> ConversationRecord | None:
    async with db.read() as conn:
        for record in await list_open_conversations(conn, user_id):
            if record.kind == PLANNING and not _idle(record):
                return record
    return None


async def link_to_active(db: Database, user_id: int, chat_message_id: int) -> None:
    """Every owner message (button prompts' answers included) belongs to the active session,
    so the assistant sees the whole exchange later."""
    record = await active(db, user_id)
    if record is not None:
        await link_incoming(db, chat_message_id, record.id)


async def link_incoming(db: Database, chat_message_id: int, conversation_id: int) -> None:
    async with db.transaction() as conn:
        await link_chat_message(conn, chat_message_id, conversation_id=conversation_id)
        await touch_conversation(conn, conversation_id)


async def record_outgoing(db: Database, user_id: int, conversation_id: int, text: str) -> None:
    """What the assistant answered in this session, kept as history for the next turns
    (`chat_messages.direction = 'out'`, same retention as the owner's messages)."""
    if not text.strip():
        return
    async with db.transaction() as conn:
        record = await get_conversation(conn, conversation_id)
        session_id = None if record is None else record.workout_session_id
        if session_id is not None and await get_workout_session(conn, session_id) is None:
            session_id = None
        await insert_chat_message(
            conn,
            user_id=user_id,
            session_id=session_id,
            direction="out",
            text=text,
            conversation_id=conversation_id,
        )
        await touch_conversation(conn, conversation_id)


async def history(
    db: Database,
    conversation_id: int,
    *,
    limit: int = HISTORY_LIMIT,
    exclude_message_id: int | None = None,
) -> list[Turn]:
    """The session's last turns, oldest first, without `exclude_message_id` (the message
    being answered right now)."""
    async with db.read() as conn:
        messages = await list_conversation_messages(conn, conversation_id, limit=limit + 1)
    kept = [message for message in messages if message.id != exclude_message_id][-limit:]
    return [
        Turn(role="user" if message.direction == "in" else "assistant", text=message.text)
        for message in kept
    ]


__all__ = [
    "HISTORY_LIMIT",
    "PLANNING",
    "PLANNING_IDLE_HOURS",
    "TRAINING",
    "Turn",
    "active",
    "close_planning",
    "current_planning",
    "history",
    "link_incoming",
    "link_to_active",
    "open_planning",
    "planning_draft_shown",
    "record_outgoing",
]
