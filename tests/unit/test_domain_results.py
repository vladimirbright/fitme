"""`domain/results.py`: `SetResult`/`ParsedResults` (`result_parse` output) and
`Recap`/`PlanChange` (`recap` output). Strict validation: finite values, positive loads,
reps >= 0, no extra fields, and `PlanChange` never carries a load."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from fitme.domain.results import ChangeReps, ParsedResults, Recap, SetResult, SwapExercise


class TestSetResult:
    def test_a_normal_completed_set_is_valid(self) -> None:
        result = SetResult(set_index=1, reps=8, load_kg=42.5)
        assert result.skipped is False

    def test_a_skipped_set_reports_nothing_else(self) -> None:
        result = SetResult(set_index=2, skipped=True)
        assert result.reps is None
        assert result.load_kg is None

    def test_a_skipped_set_with_reps_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=8, skipped=True)

    def test_a_skipped_set_with_a_load_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, load_kg=20.0, skipped=True)

    def test_a_non_skipped_set_needs_reps(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, skipped=False)

    def test_reps_below_zero_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=-1)

    def test_a_non_positive_load_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=8, load_kg=0)
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=8, load_kg=-5)

    def test_an_implausibly_high_load_is_rejected(self) -> None:
        """A coordinator-set plausibility ceiling (500 kg): no catalog exercise reaches this,
        so a parsed value above it is a hallucination, not a real (if extreme) lift."""
        SetResult(set_index=1, reps=1, load_kg=500)  # exactly at the ceiling is fine
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=1, load_kg=500.1)

    def test_bodyweight_set_has_no_load(self) -> None:
        result = SetResult(set_index=1, reps=10, load_kg=None)
        assert result.load_kg is None

    def test_non_finite_load_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=8, load_kg=math.inf)
        with pytest.raises(ValidationError):
            SetResult(set_index=1, reps=8, load_kg=math.nan)

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult.model_validate({"set_index": 1, "reps": 8, "rpe": 9})

    def test_set_index_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SetResult(set_index=0, reps=8)
        with pytest.raises(ValidationError):
            SetResult(set_index=11, reps=8)


class TestParsedResults:
    def test_valid_parsed_results(self) -> None:
        parsed = ParsedResults(
            sets=[SetResult(set_index=1, reps=8, load_kg=42.5)],
            safety_signal=False,
            unclear=False,
        )
        assert parsed.safety_signal is False
        assert parsed.unclear is False

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ParsedResults.model_validate(
                {"sets": [], "safety_signal": False, "unclear": False, "note": "hi"}
            )


class TestPlanChange:
    def test_swap_exercise_carries_no_load(self) -> None:
        change = SwapExercise(from_exercise_id="a", to_exercise_id="b")
        dumped = change.model_dump()
        assert "load" not in dumped
        assert "kg" not in dumped
        assert dumped["kind"] == "swap_exercise"

    def test_change_reps_carries_no_load(self) -> None:
        change = ChangeReps(exercise_id="a", reps_min=5, reps_max=8)
        dumped = change.model_dump()
        assert "load" not in dumped
        assert "kg" not in dumped

    def test_change_reps_rejects_min_above_max(self) -> None:
        with pytest.raises(ValidationError):
            ChangeReps(exercise_id="a", reps_min=10, reps_max=5)

    def test_swap_exercise_rejects_a_load_field(self) -> None:
        with pytest.raises(ValidationError):
            SwapExercise.model_validate(
                {"from_exercise_id": "a", "to_exercise_id": "b", "load_kg": 40.0}
            )


class TestRecap:
    def test_recap_with_no_suggestions(self) -> None:
        recap = Recap(text="Completed as planned.")
        assert recap.suggestions == []

    def test_recap_with_mixed_suggestions_round_trips_through_json(self) -> None:
        recap = Recap(
            text="Held steady this week.",
            suggestions=[
                SwapExercise(from_exercise_id="a", to_exercise_id="b"),
                ChangeReps(exercise_id="c", reps_min=6, reps_max=10),
            ],
        )
        dumped = recap.model_dump(mode="json")
        restored = Recap.model_validate(dumped)
        assert restored == recap

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Recap.model_validate({"text": "hi", "suggestions": [], "load_change": {}})

    def test_text_at_the_length_ceiling_is_accepted(self) -> None:
        Recap(text="x" * 2000)

    def test_text_over_the_length_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Recap(text="x" * 2001)
