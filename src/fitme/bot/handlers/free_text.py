"""The catch-all free-text handler (A§6.3). Registered last (`bot/app.py`'s router order),
so every `Command(...)`-filtered handler gets first refusal; this only ever sees plain text
(or a caption) that no command handler claimed.

Order (A§6.3, A§6.6): save to `chat_messages`, then the stop-word guard *before anything
else* — including a pending `/delete` confirmation or an active setup step — **except** for
the `screening_other` step (B2): that note must be persisted first, because it may set
`other_unlisted = yes` (needing clearance) regardless of whether it also halts. A halt hit
halts and nothing below it runs.

Also handles the owner's *edited* messages (`on_edited_message`): a stop word there halts
too, since editing a message is just as much "the owner reporting a symptom" as sending a new
one.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from fitme.bot.handlers.setup import handle_setup_free_text, show_step
from fitme.db.connection import Database
from fitme.i18n import t
from fitme.services import account as account_service
from fitme.services import profile as profile_service
from fitme.services.safety import record_incoming_text, scan_and_maybe_halt


async def on_free_text(
    message: Message, db: Database, user_id: int, pending_deletes: set[int]
) -> None:
    text = message.text or message.caption or ""
    await record_incoming_text(db, user_id=user_id, session_id=None, text=text)

    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    step = await profile_service.get_step(db, user_id)

    if step == "screening_other":
        stripped = text.strip()
        if not stripped:
            # Whitespace-only input behaves like Skip, not like an empty "note".
            nxt = await profile_service.skip_other_note(db, user_id)
            await show_step(message, db, user_id, nxt)
            return
        # B2 (A§6.6): the note is persisted (and the step advanced past) *before* the
        # stop-word scan, so a halting note is never lost — it may set `other_unlisted =
        # yes` (needing clearance) regardless of whether it also halts.
        nxt = await profile_service.submit_other_note(db, user_id, stripped)
        halted = await scan_and_maybe_halt(db, user_id=user_id, lang=lang, text=text)
        if halted is not None:
            await message.answer(t("halt.message", lang))
            return
        await show_step(message, db, user_id, nxt)
        return

    halted = await scan_and_maybe_halt(db, user_id=user_id, lang=lang, text=text)
    if halted is not None:
        await message.answer(t("halt.message", lang))
        return

    if user_id in pending_deletes:
        if text.strip() == "DELETE":
            pending_deletes.discard(user_id)
            await account_service.delete_user(db, user_id)
            await message.answer(t("delete.done", lang))
        else:
            await message.answer(t("delete.wrong_confirmation", lang))
        return

    if await handle_setup_free_text(message, db, user_id, text):
        return

    await message.answer(t("unknown.free_text_hint", lang))


async def on_edited_message(message: Message, db: Database, user_id: int) -> None:
    """A stop word in an edited message still halts: the safety scan is the only thing that
    reacts to an edit (no delete/setup routing here — an edit isn't a new conversational
    turn)."""
    text = message.text or message.caption or ""
    if not text:
        return
    await record_incoming_text(db, user_id=user_id, session_id=None, text=text)
    snapshot = await profile_service.get_snapshot(db, user_id)
    lang = snapshot.language
    halted = await scan_and_maybe_halt(db, user_id=user_id, lang=lang, text=text)
    if halted is not None:
        await message.answer(t("halt.message", lang))


def build_router() -> Router:
    router = Router(name="free_text")
    router.message.register(on_free_text, F.text | F.caption)
    router.edited_message.register(on_edited_message, F.text | F.caption)
    return router
