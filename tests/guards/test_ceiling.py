"""`guards.ceiling.check_ceiling` (A§7, A§7.4: "the ceiling is exceeded"; "there is no history
but a kg load is proposed"; B2: fail closed on non-finite/non-positive inputs)."""

from __future__ import annotations

import math

from fitme.guards.ceiling import check_ceiling


def test_ceiling_exceeded_is_blocked() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=105.0, increment_kg=2.5)
    assert verdict.ok is False


def test_no_history_blocks_a_kg_proposal() -> None:
    """ "With no history, only a calibration load (the catalog start) is allowed" (A§7)."""
    verdict = check_ceiling(history_max_kg=None, proposed_load_kg=20.0, increment_kg=2.5)
    assert verdict.ok is False


def test_no_history_allows_a_non_kg_calibration_load() -> None:
    verdict = check_ceiling(history_max_kg=None, proposed_load_kg=None, increment_kg=2.5)
    assert verdict.ok is True


def test_proposed_at_history_max_plus_one_increment_is_allowed() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=102.5, increment_kg=2.5)
    assert verdict.ok is True


def test_proposed_below_history_max_is_allowed() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=90.0, increment_kg=2.5)
    assert verdict.ok is True


def test_non_kg_load_always_passes_regardless_of_history() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=None, increment_kg=2.5)
    assert verdict.ok is True


def test_verdict_carries_the_rule_name() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=105.0, increment_kg=2.5)
    assert verdict.rule == "ceiling.historical_max"


def test_nan_proposed_load_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=math.nan, increment_kg=2.5)
    assert verdict.ok is False


def test_infinite_proposed_load_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=math.inf, increment_kg=2.5)
    assert verdict.ok is False


def test_zero_proposed_load_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=0.0, increment_kg=2.5)
    assert verdict.ok is False


def test_negative_proposed_load_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=-5.0, increment_kg=2.5)
    assert verdict.ok is False


def test_nan_history_max_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=math.nan, proposed_load_kg=50.0, increment_kg=2.5)
    assert verdict.ok is False


def test_non_positive_increment_is_rejected() -> None:
    verdict = check_ceiling(history_max_kg=100.0, proposed_load_kg=101.0, increment_kg=0.0)
    assert verdict.ok is False
