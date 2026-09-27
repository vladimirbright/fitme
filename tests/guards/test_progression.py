"""`guards.progression.check_weekly_increment` (A§7, A§7.4: "the weekly cap is exceeded"; B2:
fail closed on non-finite/non-positive inputs; B5: only positive `increases_7d` entries
count)."""

from __future__ import annotations

import math

from fitme.domain.catalog import Exercise
from fitme.guards.progression import check_weekly_increment


def _squat() -> Exercise:
    return Exercise.model_validate(
        {
            "id": "barbell_back_squat",
            "names": {"en": "Barbell back squat"},
            "kind": "compound",
            "equipment": ["barbell", "rack"],
            "locations": ["public_gym"],
            "loads_areas": ["knee", "lower_back"],
            "increment_kg": 2.5,
            "start": {"kind": "kg", "kg": 20.0},
            "instructions": {"en": "..."},
        }
    )


def test_weekly_cap_exceeded_is_blocked() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[2.5], proposed_increase_kg=2.5, cap_kg=2.5
    )
    assert verdict.ok is False


def test_weekly_cap_already_used_blocks_a_further_proposal() -> None:
    """A cap already fully used this week blocks a second increase (AGENTS.md §2)."""
    verdict = check_weekly_increment(
        _squat(), increases_7d=[2.5], proposed_increase_kg=1.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_within_cap_is_allowed() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=2.5, cap_kg=2.5
    )
    assert verdict.ok is True


def test_exactly_at_cap_is_allowed() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[1.0], proposed_increase_kg=1.5, cap_kg=2.5
    )
    assert verdict.ok is True


def test_verdict_carries_the_rule_name_and_a_detail() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=1.0, cap_kg=2.5
    )
    assert verdict.rule == "progression.weekly_cap"
    assert "barbell_back_squat" in verdict.detail


def test_a_lower_catalog_cap_is_honored() -> None:
    """ "catalog may set lower" (A§7): the cap passed in is authoritative, whatever the
    exercise's own increment_kg is."""
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=1.0, cap_kg=0.5
    )
    assert verdict.ok is False


def test_a_negative_increase_then_a_full_increase_is_still_blocked() -> None:
    """B5: a decrease entry (`-10.0`, e.g. a load drop logged the same week) never offsets
    the sum; only positive entries count toward the cap. `[-10]` then `+10` is a full 10 kg
    increase for cap purposes, not a net zero."""
    verdict = check_weekly_increment(
        _squat(), increases_7d=[-10.0], proposed_increase_kg=10.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_nan_proposed_increase_is_rejected() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=math.nan, cap_kg=2.5
    )
    assert verdict.ok is False


def test_infinite_proposed_increase_is_rejected() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=math.inf, cap_kg=2.5
    )
    assert verdict.ok is False


def test_zero_proposed_increase_is_rejected() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=0.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_negative_proposed_increase_is_rejected() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=-1.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_non_positive_cap_is_rejected() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[], proposed_increase_kg=1.0, cap_kg=0.0
    )
    assert verdict.ok is False


def test_nan_entry_in_increases_7d_fails_closed() -> None:
    """B1: a non-finite entry anywhere in `increases_7d` fails the whole check, rather than
    being silently dropped from the sum — a corrupted row in the append-only log must never
    make this guard *more* permissive."""
    verdict = check_weekly_increment(
        _squat(), increases_7d=[math.nan], proposed_increase_kg=1.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_infinite_entry_in_increases_7d_fails_closed() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[math.inf], proposed_increase_kg=1.0, cap_kg=2.5
    )
    assert verdict.ok is False


def test_a_nan_entry_among_otherwise_fine_entries_still_fails_closed() -> None:
    verdict = check_weekly_increment(
        _squat(), increases_7d=[1.0, math.nan], proposed_increase_kg=1.0, cap_kg=2.5
    )
    assert verdict.ok is False
