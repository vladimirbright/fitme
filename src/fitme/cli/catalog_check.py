"""`fitme catalog check` (A§11, IMPLEMENTATION_PLAN M3): validate `exercises.toml` and the
locale files, then print `content_version`.

This is reference-data validation, not an application use case, so — like
`db/migrate.py`/`db/connection.py::open_database` in `cli/commands.py` — it calls
`fitme.catalog`, `services.catalog`, `fitme.i18n` and `fitme.config.content` directly rather
than through a `services/` use case of its own. It needs no `Settings` and no database.

Strict on purpose (coordinator note, M3 round 2): a later batch of catalog content is merged
straight into `exercises.toml` without a person re-deriving every invariant by hand, and this
command (run in `make check`) is what has to catch a mistake in it.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Callable

from pydantic import ValidationError

from fitme import i18n
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.domain.catalog import (
    HOME_SELECTABLE_EQUIPMENT,
    LOCATION_DEFAULT_EQUIPMENT,
    Catalog,
    kg_loadable_problems,
    missing_contraindication_coverage,
)
from fitme.domain.enums import (
    AREA_FLAGS,
    RED_FLAGS,
    VALID_LOADS_AREAS,
    AgeBucket,
    BarbellExperience,
    Equipment,
    Experience,
    Focus,
    Location,
    Preference,
    RefusalCode,
    WeightBucket,
)
from fitme.services.catalog import available_exercises

# The movement patterns every location must offer at least one exercise for, with no
# screening flags active (IMPLEMENTATION_PLAN M3: "every location has a workable full-body
# set"). `ExercisePattern.ACCESSORY` is deliberately excluded: it's a catch-all for isolation
# work, not part of a minimal full-body session.
_DEFAULT_REQUIRED_PATTERNS: tuple[str, ...] = (
    "squat",
    "hinge",
    "horizontal_push",
    "horizontal_pull",
    "vertical_push",
    "vertical_pull",
    "core",
    "mobility",
    "conditioning",
)

# A§4.4 "honest patterns": coverage rules adapt per location rather than mislabeling data to
# satisfy a one-size-fits-all rule. `apartment_no_equipment` is the one exception: a genuine
# vertical pull needs a bar, which by definition isn't available there — a plan for that
# location compensates with (extra) horizontal pulls instead, so `vertical_pull` isn't
# required for it.
_REQUIRED_PATTERNS_BY_LOCATION: dict[Location, tuple[str, ...]] = {
    location: _DEFAULT_REQUIRED_PATTERNS for location in Location
} | {
    Location.APARTMENT_NO_EQUIPMENT: tuple(
        p for p in _DEFAULT_REQUIRED_PATTERNS if p != "vertical_pull"
    )
}

# Every enum whose values need a `t()` label key, and the dotted key prefix each value is
# nested under in the locale files (A§5.1's setup questionnaire, A§6.5's check-ins). Kept
# here, next to the CLI check, rather than in `i18n/` itself: it's specific to *which* enums
# the setup/check-in UI shows, not a property of the loader.
_ENUM_LABEL_REGISTRY: dict[str, tuple[str, ...]] = {
    "enum.age_bucket": tuple(b.value for b in AgeBucket),
    "enum.weight_bucket": tuple(b.value for b in WeightBucket),
    "enum.experience": tuple(b.value for b in Experience),
    "enum.barbell_experience": tuple(b.value for b in BarbellExperience),
    "enum.preference": tuple(b.value for b in Preference),
    "enum.focus": tuple(b.value for b in Focus),
    "enum.location": tuple(b.value for b in Location),
    "enum.equipment": tuple(b.value for b in Equipment),
    "enum.screening_red_flag": tuple(sorted(f.value for f in RED_FLAGS)),
    "enum.screening_area_flag": tuple(sorted(f.value for f in AREA_FLAGS)),
    "enum.body_area": tuple(sorted(VALID_LOADS_AREAS)),
    "enum.checkin_answer": ("fine", "worse", "pain"),  # "unknown" has no button (A§6.5)
    "refusal": tuple(c.value for c in RefusalCode),
}

# A§4.4 "no internal references": catalog and locale copy is user-visible; it must not leak
# citations to this project's own internal docs.
_FORBIDDEN_REFERENCE_MARKERS: tuple[str, ...] = ("A§", "AGENTS")


def _locale_problems() -> list[str]:
    problems = list(i18n.check_parity())
    langs = i18n.supported_languages()
    for prefix, values in _ENUM_LABEL_REGISTRY.items():
        for lang in langs:
            available = i18n.keys(lang)
            for value in values:
                full_key = f"{prefix}.{value}"
                if full_key not in available:
                    problems.append(f"missing label key {full_key!r} for language {lang!r}")
    return problems


def _equipment_for_coverage_check(location: Location) -> frozenset[Equipment]:
    """`LOCATION_DEFAULT_EQUIPMENT` has no entry for `home_equipment` (A§5.1: it's the
    user's own multi-select, not a default). This check assumes the most a home setup could
    ever offer — `HOME_SELECTABLE_EQUIPMENT`, not the full `Equipment` enum, since the
    questionnaire never offers `machine`/`cable` there — to prove every required pattern is
    *reachable* at home in principle."""
    if location == Location.HOME_EQUIPMENT:
        return HOME_SELECTABLE_EQUIPMENT
    return LOCATION_DEFAULT_EQUIPMENT[location]


def _full_body_coverage_problems(catalog: Catalog) -> list[str]:
    problems: list[str] = []
    for location in Location:
        equipment = _equipment_for_coverage_check(location)
        exercises = available_exercises(catalog, location, equipment, [])
        patterns_present = {exercise.pattern.value for exercise in exercises}
        required = _REQUIRED_PATTERNS_BY_LOCATION[location]
        missing = [p for p in required if p not in patterns_present]
        if missing:
            problems.append(f"{location.value}: missing exercise pattern(s): {', '.join(missing)}")
    return problems


def _contraindication_problems(catalog: Catalog) -> list[str]:
    return missing_contraindication_coverage(catalog)


def _catalog_text_fields(catalog: Catalog) -> list[tuple[str, str]]:
    """Every `(field description, text)` pair from the catalog worth scanning for forbidden
    references or terms: every language's `names` and `instructions` entry."""
    fields: list[tuple[str, str]] = []
    for exercise in catalog.exercise:
        for lang, text in exercise.names.items():
            fields.append((f"{exercise.id}.names.{lang}", text))
        for lang, text in exercise.instructions.items():
            fields.append((f"{exercise.id}.instructions.{lang}", text))
    return fields


def forbidden_reference_problems(catalog: Catalog) -> list[str]:
    """A§4.4 "no internal references": no catalog `names`/`instructions` string and no locale
    string may contain `"A§"` or `"AGENTS"` (this project's own internal doc-citation style).
    Exported for `tests/unit/test_forbidden_terms.py`, so the CLI check and the test share one
    implementation."""
    problems: list[str] = []
    for label, text in _catalog_text_fields(catalog):
        for marker in _FORBIDDEN_REFERENCE_MARKERS:
            if marker in text:
                problems.append(f"{label}: contains forbidden reference marker {marker!r}")
    for lang in i18n.supported_languages():
        for key in sorted(i18n.keys(lang)):
            text = i18n.t(key, lang)
            for marker in _FORBIDDEN_REFERENCE_MARKERS:
                if marker in text:
                    problems.append(
                        f"locale {lang}:{key}: contains forbidden reference marker {marker!r}"
                    )
    return problems


def catalog_check(*, load: Callable[[], Catalog] = load_catalog) -> int:
    """Validate the catalog and locales; print `content_version` on success. Returns a
    process exit code: 0 if everything checks out, 1 otherwise. Never raises for an ordinary
    content problem (a broken catalog fixture, a missing locale key) — those are reported as
    problems, not exceptions.

    `load` defaults to the real `exercises.toml` (`fitme.catalog.load_catalog`); tests pass a
    stand-in that raises on a broken fixture, to exercise this function's error handling
    without touching the shipped catalog file.
    """
    try:
        catalog = load()
    except (ValidationError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"fitme catalog check: exercises.toml is invalid: {exc}", file=sys.stderr)
        return 1

    problems = (
        _full_body_coverage_problems(catalog)
        + _contraindication_problems(catalog)
        + kg_loadable_problems(catalog)
        + _locale_problems()
        + forbidden_reference_problems(catalog)
    )
    if problems:
        print(f"fitme catalog check: {len(problems)} problem(s) found:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"catalog OK: {len(catalog.exercise)} exercise(s)")
    print(f"locales OK: {', '.join(i18n.supported_languages())}")
    print(f"content_version: {content_version()}")
    return 0
