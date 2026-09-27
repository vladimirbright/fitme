"""Activation: binding the one Telegram account to the one user row (A§6.1, ADR 0002).

`fitme activate [--rebind]` calls `issue_activation_code`; `/activate <code>` calls
`bind_telegram_account`. Both open exactly one `db.transaction()` each, so callers never
nest units of work (A§4.6 rule 4).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from fitme import clock
from fitme.db.connection import Database
from fitme.db.controllers.auth import (
    insert_activation_code,
    invalidate_pending_activation_codes,
    mark_activation_code_used,
    upsert_activation_failed_attempts,
)
from fitme.db.controllers.users import (
    delete_telegram_account,
    insert_telegram_account,
    insert_user,
)
from fitme.db.selectors.auth import get_activation_failed_attempts, list_pending_activation_codes
from fitme.db.selectors.users import get_telegram_account_by_user_id, get_the_user

# Unambiguous alphabet: no 0/O, 1/I/L (A§6.1: "random, 8+ characters"). 10 characters of this
# 31-symbol alphabet is comfortably in the "hard to brute force in 15 minutes" range, and
# still short enough to type by hand if copy-paste isn't available.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10
_CODE_TTL_MINUTES = 15
MAX_FAILED_ATTEMPTS = 5


class BindOutcome(StrEnum):
    BOUND = "bound"
    ALREADY_BOUND = "already_bound"
    INVALID_CODE = "invalid_code"
    TOO_MANY_ATTEMPTS = "too_many_attempts"


@dataclass(frozen=True, slots=True)
class BindResult:
    outcome: BindOutcome
    user_id: int | None = None


def _normalize_code(code: str) -> str:
    return code.strip().upper()


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode("utf-8")).hexdigest()


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


async def issue_activation_code(db: Database, *, rebind: bool) -> str:
    """`fitme activate [--rebind]` (A§6.1, A§11).

    Mints a fresh one-time code and invalidates any still-pending one, so there is only ever
    one code that can be redeemed. `--rebind` unlinks the currently bound Telegram account
    first (the user row itself, and its data, are kept); without `--rebind`, an already-bound
    instance still gets a new code printed, but `/activate` on the bot side refuses to use it
    (see `bind_telegram_account`) — the operator must pass `--rebind` to actually replace the
    link.
    """
    code = generate_code()
    expires_at = clock.now() + timedelta(minutes=_CODE_TTL_MINUTES)
    async with db.transaction() as conn:
        if rebind:
            user = await get_the_user(conn)
            if user is not None:
                await delete_telegram_account(conn, user.id)
        await invalidate_pending_activation_codes(conn)
        await insert_activation_code(conn, code_hash=_hash_code(code), expires_at=expires_at)
        await upsert_activation_failed_attempts(conn, 0)
    return code


async def bind_telegram_account(
    db: Database, *, code: str, telegram_user_id: int, chat_id: int, language: str
) -> BindResult:
    """`/activate <code>` (A§6.1): binds `users` + `telegram_accounts` in one transaction.

    Refuses with `ALREADY_BOUND` if an account is already linked. A wrong or expired code
    increments the shared failed-attempt counter; at `MAX_FAILED_ATTEMPTS` every pending code
    is invalidated (the operator reruns `fitme activate`) and the counter resets. The hash
    compare is constant-time (`hmac.compare_digest`) against every pending code, not a SQL
    equality lookup, so a wrong guess can't be timed against which code it's closest to.
    """
    async with db.transaction() as conn:
        existing_user = await get_the_user(conn)
        if existing_user is not None:
            existing_account = await get_telegram_account_by_user_id(conn, existing_user.id)
            if existing_account is not None:
                return BindResult(outcome=BindOutcome.ALREADY_BOUND, user_id=existing_user.id)

        submitted_hash = _hash_code(code)
        candidates = await list_pending_activation_codes(conn)
        match = next(
            (c for c in candidates if hmac.compare_digest(c.code_hash, submitted_hash)), None
        )
        if match is None or match.expires_at < clock.utc_now():
            attempts = await get_activation_failed_attempts(conn) + 1
            if attempts >= MAX_FAILED_ATTEMPTS:
                await invalidate_pending_activation_codes(conn)
                await upsert_activation_failed_attempts(conn, 0)
                return BindResult(outcome=BindOutcome.TOO_MANY_ATTEMPTS)
            await upsert_activation_failed_attempts(conn, attempts)
            return BindResult(outcome=BindOutcome.INVALID_CODE)

        await mark_activation_code_used(conn, match.id)
        await upsert_activation_failed_attempts(conn, 0)
        user_id = (
            existing_user.id
            if existing_user is not None
            else await insert_user(conn, language=language, timezone=None)
        )
        await insert_telegram_account(
            conn, user_id=user_id, telegram_user_id=telegram_user_id, chat_id=chat_id
        )
        return BindResult(outcome=BindOutcome.BOUND, user_id=user_id)
