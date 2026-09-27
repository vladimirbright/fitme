"""AGENTS.md §3 language rules as data + one check, shared by the lint-style test over every
shipped string (`tests/unit/test_forbidden_terms.py`) and the runtime check over
model-authored display text (a plan name, a workout title, a prescription note) before it is
shown (`services.planning`).

Never "trainer/coach/physio/therapist/your personal X", never a diagnosis/treatment/cure/
therapy/rehab claim, no weight-loss claims. "training" and "health" themselves are fine —
only "trainer" and (English) bare "heal*" (not "health*") are banned.

Matching notes:
- "diagnosed"/"diagnose" is deliberately NOT banned in English: the screening copy asks about
  "diagnosed high blood pressure" (self-reported history), which is not the system making a
  diagnosis claim. Russian "диагноз" is banned and doesn't collide with the verb form
  "диагностировано" used the same way (different letter after "диагно").
- Every term in `WORD_START_TERMS` is matched at a word boundary (`\bterm`, no boundary
  required at the end, so it still catches an inflected form: "trainers", "cures") rather
  than as a plain substring: "cure" plainly matches inside "se-CURE" ("a secure bar"), and
  "heal" inside "health". Multi-word phrases stay plain-substring.
"""

from __future__ import annotations

import re

WORD_START_TERMS: frozenset[str] = frozenset(
    {
        "trainer",
        "coach",
        "physio",
        "nutritionist",
        "therapy",
        "therapeutic",
        "rehab",
        "cure",
        "treat",
    }
)
FORBIDDEN_EN: tuple[str, ...] = (
    "trainer",
    "coach",
    "physio",
    "personal trainer",
    "your personal",
    "nutritionist",
    "therapy",
    "therapeutic",
    "rehab",
    "heal",
    "treat",
    "cure",
    "prevent injury",
    "weight loss",
    "fat loss",
    "lose weight",
    "burn fat",
)
FORBIDDEN_RU: tuple[str, ...] = (
    "тренер",
    "коуч",
    "персональн",
    "физиотерапевт",
    "терапи",
    "реабилит",
    "диагноз",
    "лечит",
    "лечение",
    "похудение",
    "похуде",
    "сбросить вес",
    "жиросжиг",
)
ALL_FORBIDDEN_TERMS: tuple[str, ...] = FORBIDDEN_EN + FORBIDDEN_RU

HEAL_PATTERN = re.compile(r"\bheal(?!th)\w*")


def term_present(text: str, term: str) -> bool:
    """Whether `term` (one entry of the lists above) occurs in `text`. `text` must already be
    casefolded; the lists are lowercase."""
    if term == "heal":
        return HEAL_PATTERN.search(text) is not None
    if term in WORD_START_TERMS:
        return re.search(rf"\b{re.escape(term)}", text) is not None
    return term in text


def first_forbidden_term(text: str) -> str | None:
    """The first banned term found in `text` (any language, casefolded here), or `None`.
    Model-authored text is checked against both lists whatever the interface language: a
    Russian-interface plan can carry an English word and vice versa."""
    folded = text.casefold()
    for term in ALL_FORBIDDEN_TERMS:
        if term_present(folded, term):
            return term
    return None
