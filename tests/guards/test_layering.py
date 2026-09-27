"""`guards.layering.combine_with_llm_signal` (A§7.2: LLM safety signals can only add a halt,
never remove one; A§7.4: "LLM safety_signal=false does not clear a stop-word hit")."""

from __future__ import annotations

from fitme.domain.guard_types import GuardVerdict
from fitme.guards.layering import combine_with_llm_signal


def test_llm_false_does_not_clear_a_deterministic_block() -> None:
    blocked = GuardVerdict(rule="stop_words.scan", ok=False, detail="pain reported")
    combined = combine_with_llm_signal(blocked, llm_safety_signal=False)
    assert combined.ok is False
    assert combined == blocked  # unchanged, not re-derived


def test_llm_true_does_not_clear_a_deterministic_block_either() -> None:
    blocked = GuardVerdict(rule="stop_words.scan", ok=False, detail="pain reported")
    combined = combine_with_llm_signal(blocked, llm_safety_signal=True)
    assert combined.ok is False
    assert combined == blocked


def test_llm_true_adds_a_halt_on_top_of_a_passing_verdict() -> None:
    passing = GuardVerdict(rule="ceiling.historical_max", ok=True, detail="within ceiling")
    combined = combine_with_llm_signal(passing, llm_safety_signal=True)
    assert combined.ok is False
    assert combined.rule == "layering.llm_safety_signal"


def test_llm_false_leaves_a_passing_verdict_unchanged() -> None:
    passing = GuardVerdict(rule="ceiling.historical_max", ok=True, detail="within ceiling")
    combined = combine_with_llm_signal(passing, llm_safety_signal=False)
    assert combined == passing
