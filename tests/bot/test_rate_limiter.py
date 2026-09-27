"""`_PerChatRateLimiter` (A§6.1): prunes stale entries on every hit and caps how many chats
it remembers, so it can't grow without bound over a long-running process."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fitme import clock
from fitme.bot.middleware import _MAX_TRACKED_CHATS, _PerChatRateLimiter


def test_a_second_hit_within_the_window_is_rate_limited() -> None:
    limiter = _PerChatRateLimiter(timedelta(seconds=10))

    assert limiter.hit(1) is False  # first hit: allowed
    assert limiter.hit(1) is True  # immediately again: rate-limited


def test_a_hit_after_the_window_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = _PerChatRateLimiter(timedelta(seconds=10))
    start = clock.now()
    monkeypatch.setattr(clock, "now", lambda: start)

    assert limiter.hit(1) is False

    monkeypatch.setattr(clock, "now", lambda: start + timedelta(seconds=11))
    assert limiter.hit(1) is False


def test_stale_entries_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = _PerChatRateLimiter(timedelta(seconds=10))
    start = clock.now()
    monkeypatch.setattr(clock, "now", lambda: start)
    limiter.hit(1)

    monkeypatch.setattr(clock, "now", lambda: start + timedelta(seconds=11))
    limiter.hit(2)  # this hit's own pruning pass should drop chat 1's stale entry

    assert 1 not in limiter._last_hit_at
    assert 2 in limiter._last_hit_at


def test_the_tracked_chat_count_never_exceeds_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A window long enough that nothing gets pruned by age within this test, so only the
    # explicit size cap is what keeps the dict bounded.
    limiter = _PerChatRateLimiter(timedelta(hours=1))
    start = clock.now()

    for chat_id in range(_MAX_TRACKED_CHATS + 50):
        monkeypatch.setattr(
            clock, "now", lambda chat_id=chat_id: start + timedelta(seconds=chat_id)
        )
        limiter.hit(chat_id)

    assert len(limiter._last_hit_at) <= _MAX_TRACKED_CHATS
