"""AGENTS.md §3 language rules, enforced as a lint-style test over every user-visible locale
string, every catalog exercise name, and every catalog instruction: never
"trainer/coach/physio/therapist/your personal X", never a diagnosis/treatment/cure/therapy/
rehab claim, no weight-loss claims. "training" and "health" themselves are fine — only
"trainer" and (English) bare "heal*" (not "health*") are banned.

Also checks A§4.4's "no internal references" catalog invariant (no "A§"/"AGENTS" citation
leaking into user-visible copy), sharing the same scanner `fitme catalog check` uses.
"""

from __future__ import annotations

import importlib.resources

import pytest

from fitme import i18n
from fitme.catalog import load_catalog
from fitme.cli.catalog_check import forbidden_reference_problems
from fitme.i18n import wording

# The term lists and the matcher live in `fitme.i18n.wording` (shared with the runtime check
# over model-authored plan text); this test only supplies the corpora.
_WORD_START_TERMS = wording.WORD_START_TERMS
_FORBIDDEN_EN = wording.FORBIDDEN_EN
_FORBIDDEN_RU = wording.FORBIDDEN_RU
_HEAL_PATTERN = wording.HEAL_PATTERN
_term_present = wording.term_present

# M4: prompts legitimately state, in one fixed sentence repeated verbatim across all five
# `prompts/*.v1.md` files, that the system is "not a trainer, coach, physiotherapist,
# nutritionist, or doctor" and "does not diagnose, treat, cure, or otherwise manage any
# medical condition" — a negated disclaimer that necessarily contains several of the exact
# terms `_FORBIDDEN_EN` bans. Rather than weaken the lint's matching (which is what actually
# protects user-visible copy elsewhere), this one exact sentence is allow-listed and stripped
# out of the scanned text before the forbidden-term check runs; anything else in a prompt file
# still gets caught, including the same terms used in an unlisted sentence.
_ALLOWED_PROMPT_DISCLAIMER_SENTENCES: tuple[str, ...] = (
    "this system is a training plan generator and training log, not a trainer, coach, "
    "physiotherapist, nutritionist, or doctor; it does not diagnose, treat, cure, or "
    "otherwise manage any medical condition, and it makes no weight-loss claims.",
)


def _locale_text(lang: str) -> str:
    """Every *user-visible* string value for `lang`, joined — deliberately not the raw TOML
    file text, which would also scan this file's own explanatory comments (containing the
    very words being banned) as if they were copy."""
    return "\n".join(i18n.t(key, lang) for key in sorted(i18n.keys(lang))).casefold()


def _catalog_instructions_text(lang: str) -> str:
    catalog = load_catalog()
    texts = (exercise.instructions.get(lang, "") for exercise in catalog.exercise)
    return "\n".join(texts).casefold()


def _catalog_names_text(lang: str) -> str:
    catalog = load_catalog()
    texts = (exercise.names.get(lang, "") for exercise in catalog.exercise)
    return "\n".join(texts).casefold()


def _prompts_text() -> str:
    """Every `prompts/*.md` file's raw text, joined and casefolded — mirrors
    `_locale_text`/`_catalog_instructions_text` above, and `llm.prompts`'s own
    `importlib.resources.files("fitme") / "prompts"` lookup."""
    root = importlib.resources.files("fitme") / "prompts"
    texts = [
        item.read_text(encoding="utf-8")
        for item in root.iterdir()
        if item.is_file() and item.name.endswith(".md")
    ]
    return "\n".join(texts).casefold()


def _strip_allowed_prompt_disclaimers(text: str) -> str:
    for sentence in _ALLOWED_PROMPT_DISCLAIMER_SENTENCES:
        text = text.replace(sentence, "")
    return text


@pytest.mark.parametrize("term", _FORBIDDEN_EN)
def test_english_locale_has_no_forbidden_terms(term: str) -> None:
    assert not _term_present(_locale_text("en"), term), f"forbidden term {term!r} found in en.toml"


@pytest.mark.parametrize("term", _FORBIDDEN_RU)
def test_russian_locale_has_no_forbidden_terms(term: str) -> None:
    assert not _term_present(_locale_text("ru"), term), f"forbidden term {term!r} found in ru.toml"


@pytest.mark.parametrize("term", _FORBIDDEN_EN)
def test_catalog_english_instructions_have_no_forbidden_terms(term: str) -> None:
    text = _catalog_instructions_text("en")
    assert not _term_present(text, term), (
        f"forbidden term {term!r} found in an English catalog instruction"
    )


@pytest.mark.parametrize("term", _FORBIDDEN_RU)
def test_catalog_russian_instructions_have_no_forbidden_terms(term: str) -> None:
    text = _catalog_instructions_text("ru")
    assert not _term_present(text, term), (
        f"forbidden term {term!r} found in a Russian catalog instruction"
    )


@pytest.mark.parametrize("term", _FORBIDDEN_EN)
def test_catalog_english_names_have_no_forbidden_terms(term: str) -> None:
    text = _catalog_names_text("en")
    assert not _term_present(text, term), (
        f"forbidden term {term!r} found in an English exercise name"
    )


@pytest.mark.parametrize("term", _FORBIDDEN_RU)
def test_catalog_russian_names_have_no_forbidden_terms(term: str) -> None:
    text = _catalog_names_text("ru")
    assert not _term_present(text, term), (
        f"forbidden term {term!r} found in a Russian exercise name"
    )


def test_training_is_allowed_even_though_trainer_is_not() -> None:
    text = _locale_text("en")
    assert "training" in text  # sanity: we do use the word "training" throughout
    assert not _term_present(text, "trainer")


def test_trenirovka_is_allowed_even_though_trener_is_not() -> None:
    text = _locale_text("ru")
    assert "тренировк" in text  # "training"/"workout" in Russian
    assert not _term_present(text, "тренер")


def test_health_is_allowed_even_though_heal_is_not() -> None:
    text = _locale_text("en")
    assert "health" in text  # sanity: the screening flow talks about "health" a lot
    assert not _term_present(text, "heal")


def test_heal_pattern_still_catches_inflected_forms() -> None:
    assert _HEAL_PATTERN.search("this heals the back") is not None
    assert _HEAL_PATTERN.search("a healing exercise") is not None
    assert _HEAL_PATTERN.search("your health matters") is None
    assert _HEAL_PATTERN.search("a healthy habit") is None


def test_secure_is_allowed_even_though_cure_is_not() -> None:
    """M3-round-3: a plain substring check for "cure" false-positived on "a secure bar" in
    the band lat pulldown instructions. `_WORD_START_TERMS` matches at a word boundary
    instead, so it still catches "cure"/"cures"/"cured"/"curing" without matching a word that
    merely ends in those letters."""
    assert not _term_present("loop a band over a secure bar", "cure")
    assert _term_present("there is no cure for this", "cure")
    assert _term_present("this cures everything", "cure")


def test_diagnosed_question_wording_is_present_and_not_flagged() -> None:
    """A§5.1 explicitly asks about "diagnosed high blood pressure" as a self-report
    screening question — this is intentional, not a system diagnosis claim, and confirms the
    forbidden-term list above doesn't accidentally ban it."""
    assert "diagnosed with high blood pressure" in _locale_text("en")
    assert "diagnose" not in _FORBIDDEN_EN


@pytest.mark.parametrize("term", _FORBIDDEN_EN)
def test_prompts_have_no_forbidden_terms_outside_the_allowed_disclaimer(term: str) -> None:
    text = _strip_allowed_prompt_disclaimers(_prompts_text())
    assert not _term_present(text, term), (
        f"forbidden term {term!r} found in a prompt outside the allowed disclaimer sentence"
    )


def test_the_allowed_prompt_disclaimer_sentence_actually_appears_in_every_real_prompt() -> None:
    """Guards against the allow-list going stale (e.g. a prompt edit that drifts the wording):
    if this ever fails, either update the prompt back to the shared sentence, or update
    `_ALLOWED_PROMPT_DISCLAIMER_SENTENCES` to match deliberately — never weaken the lint."""
    from fitme.llm.prompts import render_prompt

    for name in ("plan_generate", "plan_revise", "session_adjust", "result_parse", "recap"):
        text = render_prompt(name).text.casefold()
        assert any(sentence in text for sentence in _ALLOWED_PROMPT_DISCLAIMER_SENTENCES), name


def test_forbidden_terms_scanner_still_catches_a_planted_term_outside_the_disclaimer() -> None:
    planted = (
        _ALLOWED_PROMPT_DISCLAIMER_SENTENCES[0]
        + " this tool acts as your personal trainer and will cure your bad form."
    )
    stripped = _strip_allowed_prompt_disclaimers(planted.casefold())
    assert _term_present(stripped, "trainer")
    assert _term_present(stripped, "cure")


def test_no_internal_doc_references_in_the_real_catalog_and_locales() -> None:
    """A§4.4 "no internal references": no catalog name/instruction and no locale string may
    contain "A§" or "AGENTS"."""
    problems = forbidden_reference_problems(load_catalog())
    assert problems == [], problems


def test_forbidden_reference_scanner_catches_a_planted_reference() -> None:
    from fitme.domain.catalog import Catalog

    catalog = Catalog.model_validate(
        {
            "exercise": [
                {
                    "id": "planted",
                    "names": {"en": "Planted exercise (see A§4.4)", "ru": "n/a"},
                    "kind": "mobility",
                    "pattern": "mobility",
                    "equipment": [],
                    "locations": ["outdoor"],
                    "loads_areas": [],
                    "contraindicated_by": [],
                    "increment_kg": 1.0,
                    "start": {"kind": "bodyweight"},
                    "instructions": {"en": "n/a", "ru": "n/a"},
                }
            ]
        }
    )
    problems = forbidden_reference_problems(catalog)
    assert any("planted" in p for p in problems)
