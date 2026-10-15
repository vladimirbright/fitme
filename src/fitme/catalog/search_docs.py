"""Search documents for the exercise catalog (ADR 0007).

One deterministic text per exercise, built only from the validated `Exercise` fields: the
same catalog always produces the same documents and fingerprints, so the embedding index
can tell which exercises changed. No LLM is involved and nothing is invented: the catalog
has no muscle fields, so the document names the movement pattern, kind, equipment and the
body areas the exercise loads instead.

`DOCUMENT_FORMAT` is part of every fingerprint: changing how documents are built marks
every embedding stale, which is what should happen.
"""

from __future__ import annotations

import hashlib
import re

from fitme.domain.catalog import Exercise

DOCUMENT_FORMAT = 1

# Plain-language labels for the catalog's canonical codes, in both languages, so a query in
# either language can match them lexically. Codes not listed fall back to the code itself.
PATTERN_LABELS: dict[str, tuple[str, str]] = {
    "squat": ("squat, knee-dominant legs", "приседания, ноги"),
    "hinge": ("hip hinge, posterior chain", "наклон в тазобедренном суставе, задняя цепь"),
    "horizontal_push": ("horizontal push, chest press", "горизонтальный жим, грудь"),
    "horizontal_pull": ("horizontal pull, row, back", "горизонтальная тяга, спина"),
    "vertical_push": ("vertical push, overhead press, shoulders", "вертикальный жим, плечи"),
    "vertical_pull": ("vertical pull, pull-up, back", "вертикальная тяга, подтягивания, спина"),
    "core": ("core, abs, trunk", "кор, пресс, корпус"),
    "mobility": ("mobility, stretching", "мобильность, растяжка"),
    "conditioning": ("conditioning, cardio", "кардио, выносливость"),
    "accessory": ("accessory, isolation", "вспомогательное, изолирующее"),
}
KIND_LABELS: dict[str, tuple[str, str]] = {
    "compound": ("compound", "базовое"),
    "isolation": ("isolation", "изолирующее"),
    "bodyweight": ("bodyweight", "с весом тела"),
    "cardio": ("cardio", "кардио"),
    "mobility": ("mobility", "мобильность"),
}
EQUIPMENT_LABELS: dict[str, tuple[str, str]] = {
    "dumbbells": ("dumbbells", "гантели"),
    "barbell": ("barbell", "штанга"),
    "rack": ("rack", "стойка"),
    "bench": ("bench", "скамья"),
    "pull_up_bar": ("pull-up bar", "турник"),
    "kettlebell": ("kettlebell", "гиря"),
    "resistance_bands": ("resistance band", "резиновая лента"),
    "machine": ("machine", "тренажёр"),
    "cable": ("cable machine", "блочный тренажёр"),
}
AREA_LABELS: dict[str, tuple[str, str]] = {
    "neck": ("neck", "шея"),
    "shoulder": ("shoulder", "плечо"),
    "elbow_wrist": ("elbow, wrist", "локоть, запястье"),
    "lower_back": ("lower back", "поясница"),
    "hip": ("hip", "тазобедренный сустав"),
    "knee": ("knee", "колено"),
    "ankle": ("ankle", "голеностоп"),
}
_NO_EQUIPMENT = ("no equipment, bodyweight", "без оборудования, вес тела")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _labels(table: dict[str, tuple[str, str]], codes: list[str]) -> tuple[str, str]:
    pairs = [table.get(code, (code, code)) for code in codes]
    return "; ".join(en for en, _ru in pairs), "; ".join(ru for _en, ru in pairs)


def _first_sentence(text: str) -> str:
    return _SENTENCE_END.split(text.strip(), maxsplit=1)[0]


def search_document(exercise: Exercise) -> str:
    """The text that represents `exercise` in lexical and semantic search."""
    equipment = [item.value for item in exercise.equipment]
    pattern_en, pattern_ru = PATTERN_LABELS.get(
        exercise.pattern.value, (exercise.pattern.value, exercise.pattern.value)
    )
    kind_en, kind_ru = KIND_LABELS.get(exercise.kind.value, (exercise.kind.value,) * 2)
    equipment_en, equipment_ru = (
        _labels(EQUIPMENT_LABELS, equipment) if equipment else _NO_EQUIPMENT
    )
    areas_en, areas_ru = _labels(AREA_LABELS, sorted(exercise.loads_areas))
    lines = [
        f"Exercise: {exercise.names.get('en', exercise.id)}",
        f"Упражнение: {exercise.names.get('ru', exercise.id)}",
    ]
    for lang in sorted(exercise.search):
        lines.append(f"Also known as ({lang}): {exercise.search[lang]}")
    lines += [
        f"Movement pattern: {pattern_en} / {pattern_ru}",
        f"Type: {kind_en} / {kind_ru}",
        f"Equipment: {equipment_en} / {equipment_ru}",
    ]
    if areas_en:
        lines.append(f"Loads: {areas_en} / {areas_ru}")
    for lang in ("en", "ru"):
        if exercise.instructions.get(lang):
            lines.append(f"Setup ({lang}): {_first_sentence(exercise.instructions[lang])}")
    return "\n".join(lines)


def document_hash(document: str) -> str:
    """Fingerprint of one search document, including the document format version."""
    digest = hashlib.sha256(f"format={DOCUMENT_FORMAT}\n{document}".encode())
    return digest.hexdigest()


def lexical_fields(exercise: Exercise) -> tuple[str, str]:
    """`(names, body)` for the full-text index: names and search terms are kept apart from
    the rest so they can be weighted higher. The body leaves out the "Loads:" line: a query
    such as "no lower back load" would otherwise match exactly the exercises that load it.
    Avoiding an area is a structured filter (`ExerciseQuery.avoid_areas`), not a text match."""
    names = " | ".join(
        [exercise.id.replace("_", " "), *exercise.names.values(), *exercise.search.values()]
    )
    body = "\n".join(
        line for line in search_document(exercise).splitlines() if not line.startswith("Loads:")
    )
    return names, body
