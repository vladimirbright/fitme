"""Website authentication (A§9.1, A§9.2): a Telegram-delivered one-time code, sha256-hashed
web sessions, and HMAC CSRF tokens bound to the session.

`request_login_code`/`verify_login_code` never care whether an account is bound: A§9.1 "the
response is identical whether or not an account is bound" is enforced by the *caller* (the
route always returns the same generic response), not by this module refusing to run — this
module simply does nothing (no code, no rate-limit bookkeeping) when there is no bound user,
which already produces no observable difference to the caller.

Sending the code is injected as `SendCode` (a plain async callable), so this module never
imports aiogram: the website and the bot share one `Bot` instance (`fitme serve`, A§3), and
the web app is handed a closure over it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from fitme import clock, i18n
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.auth import (
    delete_web_session,
    increment_login_code_attempts,
    insert_login_code,
    insert_web_session,
    mark_login_code_used,
    touch_web_session,
)
from fitme.db.records import UserRecord, WebSessionRecord
from fitme.db.selectors.auth import (
    count_login_codes_since,
    get_latest_unused_login_code,
    get_web_session,
    list_web_sessions_for_user,
)
from fitme.db.selectors.users import get_telegram_account_by_user_id, get_the_user

# A§9.1 rate limit: 1 request per 60 seconds, 5 per hour.
_RATE_LIMIT_SHORT = timedelta(seconds=60)
_RATE_LIMIT_SHORT_MAX = 1
_RATE_LIMIT_LONG = timedelta(hours=1)
_RATE_LIMIT_LONG_MAX = 5

_CODE_TTL_MINUTES = 5  # A§9.1 "5-minute TTL"
_MAX_VERIFY_ATTEMPTS = 5  # A§9.1 "5 attempts, then invalid"

# A§9.2 session lifetime.
_SESSION_IDLE = timedelta(days=14)
_SESSION_ABSOLUTE = timedelta(days=30)
_TOUCH_INTERVAL = timedelta(minutes=5)

_SESSION_TOKEN_BYTES = 32

_logger = logging.getLogger(__name__)

SendCode = Callable[[int, str], Awaitable[None]]


class RequestCodeStatus(StrEnum):
    SENT = "sent"  # a code was generated and handed to `send_code`
    NO_ACCOUNT = "no_account"  # nothing to send to; the caller replies the same as SENT
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class RequestCodeResult:
    status: RequestCodeStatus


class VerifyStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"  # wrong code, expired, too many attempts, or nothing pending
    NO_ACCOUNT = "no_account"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    status: VerifyStatus
    session_token: str | None = None
    user_id: int | None = None


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_otp() -> str:
    """A 6-digit numeric code (A§9.1), including a leading zero."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def _single_user(db: Database) -> UserRecord | None:
    async with db.read() as conn:
        return await get_the_user(conn)


async def request_login_code(db: Database, *, send_code: SendCode) -> RequestCodeResult:
    """A§9.1 `POST /auth/request-code`. Rate limiting and "was a code actually sent" are both
    internal to this function; the route must map every status other than an unexpected
    exception to the same generic response, so a caller can't tell bound from unbound.

    The rate-limit count and the new code's insert happen in **one** `db.transaction()` (M9
    review, "ALSO" #5): reading the count and inserting as two separate units would let two
    concurrent requests each read the same (stale) count and both pass the check, admitting
    one more code than the limit allows. `Database` serializes whole units of work, not
    individual statements, so only merging them into one unit actually closes the race.

    A failure to reach Telegram (`send_code`, a network call) is caught and logged, never
    raised (M9 review, "ALSO" #2): the code is already durably stored, and the caller's
    response is the same generic message regardless, so a delivery failure must not surface
    as a 500.
    """
    user = await _single_user(db)
    if user is None:
        return RequestCodeResult(status=RequestCodeStatus.NO_ACCOUNT)

    now = clock.now()
    async with db.transaction() as conn:
        account = await get_telegram_account_by_user_id(conn, user.id)
        if account is None:
            return RequestCodeResult(status=RequestCodeStatus.NO_ACCOUNT)
        recent_short = await count_login_codes_since(
            conn, user.id, since=clock.format_timestamp(now - _RATE_LIMIT_SHORT)
        )
        recent_long = await count_login_codes_since(
            conn, user.id, since=clock.format_timestamp(now - _RATE_LIMIT_LONG)
        )
        if recent_short >= _RATE_LIMIT_SHORT_MAX or recent_long >= _RATE_LIMIT_LONG_MAX:
            return RequestCodeResult(status=RequestCodeStatus.RATE_LIMITED)

        code = generate_otp()
        expires_at = now + timedelta(minutes=_CODE_TTL_MINUTES)
        await insert_login_code(
            conn, user_id=user.id, code_hash=_hash_code(code), expires_at=expires_at
        )

    message = i18n.t("web.login.otp_message", user.language, code=code, minutes=_CODE_TTL_MINUTES)
    try:
        await send_code(account.chat_id, message)
    except Exception:
        _logger.exception("failed to send the login code to Telegram")
    return RequestCodeResult(status=RequestCodeStatus.SENT)


async def verify_login_code(db: Database, *, code: str) -> VerifyResult:
    """A§9.1 `POST /auth/verify`. Looks the code up by user_id + latest unused (the M1 note),
    not by a hash equality lookup, and compares with `hmac.compare_digest`. A wrong guess
    increments `attempts`; at `_MAX_VERIFY_ATTEMPTS` the code is marked used (so it can never
    be retried) and every subsequent attempt reports `INVALID` with nothing left to try."""
    user = await _single_user(db)
    if user is None:
        return VerifyResult(status=VerifyStatus.NO_ACCOUNT)

    async with db.transaction() as conn:
        pending = await get_latest_unused_login_code(conn, user.id)
        if pending is None:
            return VerifyResult(status=VerifyStatus.INVALID)
        if pending.attempts >= _MAX_VERIFY_ATTEMPTS or pending.expires_at < clock.utc_now():
            if pending.attempts >= _MAX_VERIFY_ATTEMPTS:
                await mark_login_code_used(conn, pending.id)
            return VerifyResult(status=VerifyStatus.INVALID)
        if not hmac.compare_digest(pending.code_hash, _hash_code(code)):
            await increment_login_code_attempts(conn, pending.id)
            return VerifyResult(status=VerifyStatus.INVALID)
        await mark_login_code_used(conn, pending.id)
        token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
        await insert_web_session(
            conn,
            id_hash=_hash_session_token(token),
            user_id=user.id,
            expires_at=clock.now() + _SESSION_ABSOLUTE,
        )
    return VerifyResult(status=VerifyStatus.OK, session_token=token, user_id=user.id)


@dataclass(frozen=True, slots=True)
class SessionState:
    user_id: int
    id_hash: str


async def resolve_session(db: Database, token: str) -> SessionState | None:
    """`None` for a missing, expired (absolute or idle) session; otherwise touches
    `last_seen_at` (at most once per `_TOUCH_INTERVAL`, A§9.2) and returns the session's user.
    """
    id_hash = _hash_session_token(token)
    async with db.read() as conn:
        record = await get_web_session(conn, id_hash)
    if record is None:
        return None
    now = clock.now()
    if now >= clock.parse_timestamp(record.expires_at):
        return None
    if now - clock.parse_timestamp(record.last_seen_at) >= _SESSION_IDLE:
        return None
    if now - clock.parse_timestamp(record.last_seen_at) >= _TOUCH_INTERVAL:
        async with db.transaction() as conn:
            await touch_web_session(conn, id_hash)
    return SessionState(user_id=record.user_id, id_hash=id_hash)


async def create_dev_session(db: Database, user_id: int) -> str:
    """Used only by tests that need a logged-in session without going through the OTP flow."""
    token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    async with db.transaction() as conn:
        await insert_web_session(
            conn,
            id_hash=_hash_session_token(token),
            user_id=user_id,
            expires_at=clock.now() + _SESSION_ABSOLUTE,
        )
    return token


async def logout(db: Database, token: str) -> None:
    async with db.transaction() as conn:
        await delete_web_session(conn, _hash_session_token(token))


async def list_sessions(db: Database, user_id: int) -> list[WebSessionRecord]:
    async with db.read() as conn:
        return await list_web_sessions_for_user(conn, user_id)


async def revoke_session(db: Database, user_id: int, id_hash: str) -> bool:
    """Revoke one of this user's own sessions. `False` (no-op) for an id_hash that isn't
    theirs, so a forged value from another session can't be used to guess-and-revoke."""
    async with db.transaction() as conn:
        record = await get_web_session(conn, id_hash)
        if record is None or record.user_id != user_id:
            return False
        await delete_web_session(conn, id_hash)
    return True


# --- CSRF (A§9.2) -------------------------------------------------------------------------------


def csrf_token(secret_key: str, session_id_hash: str) -> str:
    """An HMAC of the session id, bound to `FITME_SECRET_KEY` (A§9.2). Deterministic per
    session, so it needs no server-side storage: the same session always recomputes the same
    token, and a form simply carries it as a hidden field."""
    return hmac.new(
        secret_key.encode("utf-8"), session_id_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf(settings: Settings, session_id_hash: str, submitted: str | None) -> bool:
    if not submitted:
        return False
    expected = csrf_token(settings.secret_key.get_secret_value(), session_id_hash)
    return hmac.compare_digest(expected, submitted)


__all__ = [
    "RequestCodeResult",
    "RequestCodeStatus",
    "SendCode",
    "SessionState",
    "VerifyResult",
    "VerifyStatus",
    "create_dev_session",
    "csrf_token",
    "generate_otp",
    "list_sessions",
    "logout",
    "request_login_code",
    "resolve_session",
    "revoke_session",
    "verify_csrf",
    "verify_login_code",
]
