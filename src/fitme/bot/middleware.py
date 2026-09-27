"""`OwnerGateMiddleware` (A§6.1): runs first on every update, messages and callback queries
alike (registered on `dispatcher.update.outer_middleware`, which wraps every event type).

For the bound owner, this only annotates `data["user_id"]` and lets the update through. For
anyone else: `/activate <code>` is allowed while no account is bound yet (itself rate-limited
per chat, so brute-forcing codes can't be sped up by spamming); everything else gets one
fixed, localized reply, rate-limited per chat so a spammer can't make the bot spam back.
Non-private chats (groups, channels, ...) are ignored entirely — no reply, no storage — even
for the bound owner, who has no business talking to this bot from a group.

**Nothing is stored and nothing is logged about an unknown sender — no id, no text, not even
at DEBUG (AGENTS.md §5).** This module must never call `chat_messages` inserts or `logging`
with the sender's id or message text.

`StopWordCommandArgsMiddleware` is a separate, *inner* middleware (registered on the root
`message` observer, so it covers every command handler in every router) that scans a matched
command's own arguments for stop words, e.g. `/start my chest hurts` (A§6.3). It only ever
sees the bound owner's commands: `user_id` is absent for anything `OwnerGateMiddleware` let
through pre-bind (`/activate`), so there is nothing of the owner's to scan yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.filters import CommandObject
from aiogram.types import Message, TelegramObject, Update

from fitme import clock
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.selectors.users import (
    any_telegram_account_bound,
    get_telegram_account_by_telegram_user_id,
)
from fitme.i18n import resolve_language, t
from fitme.services import profile as profile_service
from fitme.services.safety import record_incoming_text, scan_and_maybe_halt

_REPLY_RATE_LIMIT = timedelta(minutes=10)
_ACTIVATE_RATE_LIMIT = timedelta(seconds=10)
_ACTIVATE_COMMAND = "/activate"
_MAX_TRACKED_CHATS = 1000


def _is_activate_command(text: str | None) -> bool:
    if text is None:
        return False
    first_word = text.strip().split(None, 1)[0] if text.strip() else ""
    return first_word.split("@", 1)[0].lower() == _ACTIVATE_COMMAND


class _PerChatRateLimiter:
    """At most one "pass" per `window` per chat id, in memory only (never logged or
    persisted). Prunes entries older than `window` on every call and caps how many chats it
    remembers (`_MAX_TRACKED_CHATS`), so neither a long-running process nor a spray of
    distinct chat ids can grow this dict without bound."""

    def __init__(self, window: timedelta) -> None:
        self._window = window
        self._last_hit_at: dict[int, datetime] = {}

    def hit(self, chat_id: int) -> bool:
        """True if `chat_id` is currently rate-limited (no new hit recorded); False if this
        call is allowed (and is itself now recorded)."""
        now = clock.now()
        self._prune(now)
        last = self._last_hit_at.get(chat_id)
        if last is not None and now - last < self._window:
            return True
        self._last_hit_at[chat_id] = now
        if len(self._last_hit_at) > _MAX_TRACKED_CHATS:
            oldest_chat_id = min(self._last_hit_at, key=lambda cid: self._last_hit_at[cid])
            del self._last_hit_at[oldest_chat_id]
        return False

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        stale = [chat_id for chat_id, hit_at in self._last_hit_at.items() if hit_at < cutoff]
        for chat_id in stale:
            del self._last_hit_at[chat_id]


class OwnerGateMiddleware(BaseMiddleware):
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._reply_limiter = _PerChatRateLimiter(_REPLY_RATE_LIMIT)
        self._activate_limiter = _PerChatRateLimiter(_ACTIVATE_RATE_LIMIT)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or chat is None:
            # No identifiable sender (e.g. a channel post): nothing for this gate to check.
            return await handler(event, data)

        if chat.type != "private":
            # Non-private chats are out of scope entirely (ADR 0002: single-user, no group
            # use case) — ignored even for the bound owner, no reply and nothing stored.
            return None

        async with self._db.read() as conn:
            account = await get_telegram_account_by_telegram_user_id(conn, user.id)

        if account is not None and account.telegram_user_id == user.id:
            data["user_id"] = account.user_id
            return await handler(event, data)

        message = event.message
        if message is not None and _is_activate_command(message.text):
            async with self._db.read() as conn:
                bound = await any_telegram_account_bound(conn)
            if not bound:
                if self._activate_limiter.hit(chat.id):
                    # Rate-limited: reply once per (the longer) reply window, not once per
                    # throttled attempt — reusing `_reply_limiter` means a spammer sending
                    # many attempts in a row still only ever gets one "please wait".
                    if not self._reply_limiter.hit(chat.id):
                        lang = resolve_language(user.language_code)
                        await message.answer(t("activation.rate_limited", lang))
                    return None
                return await handler(event, data)

        if message is None:
            # A callback query or edited message (or anything else) from a stranger: there
            # is no keyboard/prior message a stranger could legitimately be reacting to, so
            # this is unreachable in practice. Drop it silently rather than risk logging
            # anything about the sender.
            return None

        if self._reply_limiter.hit(chat.id):
            return None
        lang = resolve_language(user.language_code)
        text = t("start.unbound", lang, source_url=self._settings.source_url)
        await message.answer(text)
        return None


class StopWordCommandArgsMiddleware(BaseMiddleware):
    """An inner middleware (A§6.3): scans a matched command's arguments for stop words
    before the real handler runs. Register once, on the root `message` observer
    (`dp.message.middleware(...)`), so it covers every command handler across every included
    router without each one repeating the check.

    `/activate`'s argument is a one-time activation code, never scanned or recorded here
    (A§4.1: codes are never stored in plaintext) — `cmd_activate` handles it directly, and
    even the *bound* owner re-running `/activate` (e.g. by habit) must not have the code end
    up in `chat_messages`.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = data.get("user_id")
        command = data.get("command")
        is_activate = isinstance(command, CommandObject) and command.command == "activate"
        args = command.args if isinstance(command, CommandObject) else None
        if isinstance(user_id, int) and args and not is_activate and isinstance(event, Message):
            snapshot = await profile_service.get_snapshot(self._db, user_id)
            lang = snapshot.language
            await record_incoming_text(self._db, user_id=user_id, session_id=None, text=args)
            halted = await scan_and_maybe_halt(self._db, user_id=user_id, lang=lang, text=args)
            if halted is not None:
                await event.answer(t("halt.message", lang))
                return None
        return await handler(event, data)
