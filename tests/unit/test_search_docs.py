"""ADR 0007: search documents are built deterministically from catalog fields only."""

from __future__ import annotations

from fitme.catalog import load_catalog
from fitme.catalog.search_docs import (
    DOCUMENT_FORMAT,
    document_hash,
    lexical_fields,
    search_document,
)
from fitme.domain.catalog import Exercise


def _bench() -> Exercise:
    exercise = load_catalog().by_id("dumbbell_bench_press")
    assert exercise is not None
    return exercise


def test_document_has_english_and_russian_names_pattern_kind_and_equipment() -> None:
    document = search_document(_bench())
    assert "Exercise: Dumbbell bench press" in document
    assert "Упражнение: Жим гантелей лёжа" in document
    assert "horizontal push" in document and "горизонтальный жим" in document
    assert "compound / базовое" in document
    assert "dumbbells; bench / гантели; скамья" in document
    assert "Loads: elbow, wrist; shoulder" in document


def test_document_keeps_only_the_first_instruction_sentence() -> None:
    exercise = _bench()
    document = search_document(exercise)
    assert "Lie on the bench, a dumbbell in each hand over your chest." in document
    assert exercise.instructions["en"] not in document  # the full text doesn't dominate


def test_documents_and_fingerprints_are_stable() -> None:
    catalog = load_catalog()
    first = {e.id: document_hash(search_document(e)) for e in catalog.exercise}
    second = {e.id: document_hash(search_document(e)) for e in catalog.exercise}
    assert first == second
    assert len(set(first.values())) == len(first)  # every exercise has its own document


def test_fingerprint_changes_with_the_document_and_with_the_format() -> None:
    exercise = _bench()
    renamed = exercise.model_copy(update={"names": {"en": "Other", "ru": "Другое"}})
    assert document_hash(search_document(exercise)) != document_hash(search_document(renamed))
    assert f"format={DOCUMENT_FORMAT}" not in search_document(exercise)


def test_optional_fields_are_handled() -> None:
    squat = load_catalog().by_id("bodyweight_squat")
    assert squat is not None and not squat.equipment
    document = search_document(squat.model_copy(update={"search": {}}))
    assert "no equipment, bodyweight / без оборудования, вес тела" in document
    assert "Also known as" not in document
    carry = load_catalog().by_id("dumbbell_suitcase_carry")
    assert carry is not None and carry.search
    assert "Also known as (ru): suitcase carry с гантелью" in search_document(carry)


def test_full_text_body_leaves_out_loaded_areas() -> None:
    names, body = lexical_fields(_bench())
    assert "Жим гантелей лёжа" in names and "dumbbell bench press" in names
    assert "Loads:" not in body
    assert "horizontal push" in body
