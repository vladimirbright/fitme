"""UTC timestamp helpers (A§4.2).

All timestamp columns are TEXT in one format: UTC ``YYYY-MM-DDTHH:MM:SS.ffffffZ``, always
with 6 fractional digits, so lexical order equals time order. Only this module produces or
formats these strings; controllers default to `utc_now()` internally and never accept a
free-form string from a caller. Where a timestamp is semantically meaningful to a caller
(e.g. a code's `expires_at`), the caller passes a `datetime` from `now()`/arithmetic on it,
and the db layer formats it with `format_timestamp()` on the way in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Render a datetime in the canonical format. `value` must be timezone-aware."""
    if value.tzinfo is None:
        raise ValueError("format_timestamp requires a timezone-aware datetime")
    return value.astimezone(UTC).strftime(_FORMAT)


def utc_now() -> str:
    """The current time, already formatted. This is what controllers default to."""
    return format_timestamp(now())


def days_ago(days: int) -> datetime:
    """A point `days` in the past. Used for retention cutoffs."""
    return now() - timedelta(days=days)


def parse_timestamp(value: str) -> datetime:
    """The inverse of `format_timestamp`: parse the canonical UTC string back into a
    timezone-aware `datetime`. Used where a stored timestamp needs to be compared or
    converted (e.g. to the user's own timezone for the hold-clearing "not before tomorrow"
    check, A§6.6), not just compared lexically."""
    return datetime.strptime(value, _FORMAT).replace(tzinfo=UTC)
