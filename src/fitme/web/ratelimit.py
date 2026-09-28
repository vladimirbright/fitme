"""An in-memory, per-key sliding-window rate limiter (M9 review, "ALSO" #1): used to throttle
`/auth/verify` by client IP, ahead of (and independent from) the per-code attempt counter
already enforced by `services.webauth.verify_login_code`. Same style as the bot's own
`bot.middleware._PerChatRateLimiter`: prunes stale entries on every call and caps how many
keys it remembers, so neither a long-running process nor a spray of distinct keys grows it
without bound. Process-local and in-memory by design — this is a personal, single-process
instance (AGENTS.md §1), not a multi-worker deployment that would need a shared store.
"""

from __future__ import annotations

from datetime import timedelta

from fitme import clock

_MAX_TRACKED_KEYS = 1000


class SlidingWindowLimiter:
    """At most `max_count` hits per `window` per key."""

    def __init__(self, window: timedelta, max_count: int) -> None:
        self._window = window
        self._max_count = max_count
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str) -> bool:
        """True if `key` is currently rate-limited (this attempt is not recorded and should
        be refused); False if it's allowed (and is now recorded)."""
        now = clock.now().timestamp()
        cutoff = now - self._window.total_seconds()
        timestamps = [t for t in self._hits.get(key, []) if t >= cutoff]
        limited = len(timestamps) >= self._max_count
        if not limited:
            timestamps.append(now)
        self._hits[key] = timestamps
        self._prune_keys(cutoff)
        return limited

    def _prune_keys(self, cutoff: float) -> None:
        empty = [key for key, timestamps in self._hits.items() if not timestamps]
        for key in empty:
            del self._hits[key]
        if len(self._hits) > _MAX_TRACKED_KEYS:
            oldest_key = min(self._hits, key=lambda key: max(self._hits[key], default=0.0))
            del self._hits[oldest_key]


__all__ = ["SlidingWindowLimiter"]
