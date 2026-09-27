"""`fitme.i18n.wording`: the AGENTS.md §3 term lists as a runtime check over model-authored
display text (the lint over shipped strings in `test_forbidden_terms.py` shares them)."""

from __future__ import annotations

from fitme.i18n import wording


def test_first_forbidden_term_catches_english_and_russian_regardless_of_case() -> None:
    assert wording.first_forbidden_term("Your Personal Trainer plan") == "trainer"
    assert wording.first_forbidden_term("Программа от тренера") == "тренер"
    assert wording.first_forbidden_term("Heals your back") == "heal"
    assert wording.first_forbidden_term("Fat Loss Blast") == "fat loss"


def test_first_forbidden_term_leaves_ordinary_training_text_alone() -> None:
    for text in (
        "Full body strength",
        "Lower body, health check-in first",
        "Loop the band over a secure bar",
        "Тренировка ног",
        "Упор на технику",
    ):
        assert wording.first_forbidden_term(text) is None, text


def test_all_forbidden_terms_covers_both_lists() -> None:
    assert set(wording.ALL_FORBIDDEN_TERMS) == set(wording.FORBIDDEN_EN) | set(wording.FORBIDDEN_RU)
