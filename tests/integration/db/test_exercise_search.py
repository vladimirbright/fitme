"""ADR 0007: hybrid retrieval, substitutes and the plan candidate pool, over the real catalog
and a real migrated DB. Hard filters come from `available_exercises`; embeddings are faked."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from fitme.catalog import load_catalog
from fitme.config.embeddings import EmbeddingSettings
from fitme.db.connection import Database
from fitme.domain.catalog import HOME_SELECTABLE_EQUIPMENT, LOCATION_DEFAULT_EQUIPMENT
from fitme.domain.enums import (
    Equipment,
    ExerciseKind,
    ExercisePattern,
    Focus,
    Location,
    Preference,
    Quality,
    ScreeningFlag,
    Sport,
)
from fitme.domain.screening import ScreeningFlagState
from fitme.llm.embeddings import EmbeddingError
from fitme.services import exercise_search as es
from fitme.services.catalog import available_exercises
from fitme.services.catalog_index import build_index_file, sync_database


class FakeEmbedder:
    def __init__(self, model: str = "fake-1", fail: bool = False) -> None:
        self.model = model
        self.fail = fail
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingError("provider down")
        return [self.vector(text) for text in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        vector = [0.0] * 64
        for word in text.lower().replace("/", " ").split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
        vector[0] += 0.01
        return vector


_SETTINGS = EmbeddingSettings(FITME_EMBEDDINGS_MODEL="fake-1")


async def _runtime(
    db: Database, tmp_path: Path, *, embedder: FakeEmbedder | None = None, vectors: bool = True
) -> es.SearchRuntime:
    catalog = load_catalog()
    path = tmp_path / "index.sqlite"
    if vectors:
        await build_index_file(catalog, FakeEmbedder(), path)
    await sync_database(db, catalog, path)
    return await es.load_runtime(db, catalog, _SETTINGS, embedder)


def _gym() -> list:
    catalog = load_catalog()
    return available_exercises(
        catalog, Location.PUBLIC_GYM, LOCATION_DEFAULT_EQUIPMENT[Location.PUBLIC_GYM], []
    )


def _ids(results: Sequence[es.ExerciseSearchResult]) -> list[str]:
    return [result.exercise_id for result in results]


# --- search ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("bench press", {"dumbbell_bench_press", "barbell_bench_press"}),
        ("жим гантелей", {"dumbbell_bench_press"}),
        ("romanian deadlift", {"barbell_romanian_deadlift", "dumbbell_romanian_deadlift"}),
        ("румынская тяга", {"barbell_romanian_deadlift", "dumbbell_romanian_deadlift"}),
        ("подтягивания", {"pull_up"}),
    ],
)
async def test_english_and_russian_queries_find_the_exercise(
    db: Database, tmp_path: Path, query: str, expected: set[str]
) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    found = _ids(await es.search(db, runtime, _gym(), es.ExerciseQuery(text=query, limit=3)))
    assert expected & set(found[:2]), found


async def test_an_exact_name_is_ranked_first(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    results = await es.search(db, runtime, _gym(), es.ExerciseQuery(text="Жим штанги лёжа"))
    assert results[0].exercise_id == "barbell_bench_press"
    assert "exact name match" in results[0].match_reasons


async def test_a_body_region_selects_its_patterns(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    results = await es.search(db, runtime, _gym(), es.ExerciseQuery(text="спина", limit=6))
    catalog = load_catalog()
    patterns = {catalog.by_id(i).pattern for i in _ids(results)}  # type: ignore[union-attr]
    assert patterns <= {ExercisePattern.HORIZONTAL_PULL, ExercisePattern.VERTICAL_PULL}


async def test_structured_filters_work_without_text(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    query = es.ExerciseQuery(
        patterns=frozenset({ExercisePattern.VERTICAL_PULL}),
        kinds=frozenset({ExerciseKind.BODYWEIGHT}),
        limit=50,
    )
    results = await es.search(db, runtime, _gym(), query)
    catalog = load_catalog()
    assert results
    for exercise_id in _ids(results):
        exercise = catalog.by_id(exercise_id)
        assert exercise is not None
        assert exercise.pattern == ExercisePattern.VERTICAL_PULL
        assert exercise.kind == ExerciseKind.BODYWEIGHT


async def test_equipment_and_location_are_hard_filters(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, embedder=FakeEmbedder())
    catalog = load_catalog()
    dumbbells_only = available_exercises(
        catalog, Location.HOME_EQUIPMENT, frozenset({Equipment.DUMBBELLS}), []
    )
    results = await es.search(
        db, runtime, dumbbells_only, es.ExerciseQuery(text="barbell bench press", limit=50)
    )
    for exercise_id in _ids(results):
        exercise = catalog.by_id(exercise_id)
        assert exercise is not None and set(exercise.equipment) <= {Equipment.DUMBBELLS}
    assert "barbell_bench_press" not in _ids(results)


async def test_exclusions_and_avoided_areas_are_hard_filters(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    query = es.ExerciseQuery(
        text="row",
        excluded=frozenset({"dumbbell_row"}),
        avoid_areas=frozenset({"lower_back"}),
        limit=50,
    )
    results = await es.search(db, runtime, _gym(), query)
    catalog = load_catalog()
    assert results and "dumbbell_row" not in _ids(results)
    for exercise_id in _ids(results):
        assert "lower_back" not in catalog.by_id(exercise_id).loads_areas  # type: ignore[union-attr]


async def test_no_match_returns_nothing(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    assert await es.search(db, runtime, _gym(), es.ExerciseQuery(text="xyzzy qwerty")) == []
    assert await es.search(db, runtime, [], es.ExerciseQuery(text="bench press")) == []


async def test_semantic_results_are_fused_with_lexical_ones(db: Database, tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    runtime = await _runtime(db, tmp_path, embedder=embedder)
    results = await es.search(db, runtime, _gym(), es.ExerciseQuery(text="bench press", limit=5))
    assert embedder.calls == 1
    top = results[0]
    assert any(r.startswith("full-text") for r in top.match_reasons)
    assert any(r.startswith("semantic") for r in top.match_reasons)


async def test_a_provider_failure_falls_back_to_lexical(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, embedder=FakeEmbedder(fail=True))
    results = await es.search(db, runtime, _gym(), es.ExerciseQuery(text="bench press"))
    assert "barbell_bench_press" in _ids(results)
    assert not any(r.startswith("semantic") for res in results for r in res.match_reasons)


async def test_vectors_of_another_model_are_not_used(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, embedder=FakeEmbedder(model="fake-2"))
    results = await es.search(db, runtime, _gym(), es.ExerciseQuery(text="bench press"))
    assert not any(r.startswith("semantic") for res in results for r in res.match_reasons)


_BULK = [
    "back squat",
    "bench press",
    "жим гантелей",
    "romanian deadlift",
    "dumbbell row",
    "подтягивания",
    "plank",
    "спина",
    "",
    "xyzzy qwerty",
]


async def test_search_many_embeds_every_query_in_one_request(db: Database, tmp_path: Path) -> None:
    """A bulk lookup is one embedding request for all its queries (the 2026-10-18 finding:
    one request per name), and each query ranks exactly as `search` ranks it alone."""
    embedder = FakeEmbedder()
    runtime = await _runtime(db, tmp_path, embedder=embedder)
    bulk = await es.search_many(db, runtime, _gym(), _BULK, limit=8)
    assert embedder.calls == 1
    assert len(bulk) == len(_BULK)
    for text, hits in zip(_BULK, bulk, strict=True):
        alone = await es.search(db, runtime, _gym(), es.ExerciseQuery(text=text, limit=8))
        assert hits == alone, text
    assert any(r.startswith("semantic") for r in bulk[0][0].match_reasons)


async def test_search_many_falls_back_to_lexical_on_a_provider_failure(
    db: Database, tmp_path: Path
) -> None:
    embedder = FakeEmbedder(fail=True)
    runtime = await _runtime(db, tmp_path, embedder=embedder)
    bulk = await es.search_many(db, runtime, _gym(), ["bench press", "dumbbell row"], limit=8)
    assert embedder.calls == 1
    assert "barbell_bench_press" in _ids(bulk[0]) and "dumbbell_row" in _ids(bulk[1])
    assert not any(r.startswith("semantic") for hits in bulk for h in hits for r in h.match_reasons)


async def test_search_many_without_text_skips_the_embedder(db: Database, tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    runtime = await _runtime(db, tmp_path, embedder=embedder)
    (listed,) = await es.search_many(db, runtime, _gym(), [""], limit=20)
    assert embedder.calls == 0
    assert len(listed) == 20


def test_fts_expression_handles_russian_endings_and_plurals() -> None:
    assert es.fts_expression("гантелей") == '"гантел"*'
    assert es.fts_expression("спины") == '"спин"*'
    assert es.fts_expression("biceps curls") == '"bicep"* OR "curl"*'
    assert es.fts_expression("exercise without a") is None
    assert es.fts_expression('" OR *') is None


# --- substitutes ----------------------------------------------------------------------------


async def test_barbell_bench_press_becomes_dumbbell_bench_press(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path)
    home = available_exercises(
        load_catalog(),
        Location.HOME_EQUIPMENT,
        frozenset({Equipment.DUMBBELLS, Equipment.BENCH}),
        [],
    )
    results = es.find_substitutes(runtime, "barbell_bench_press", home)
    assert results[0].exercise_id == "dumbbell_bench_press" and results[0].close
    catalog = load_catalog()
    for exercise_id in _ids(results):
        exercise = catalog.by_id(exercise_id)
        assert exercise is not None and exercise.pattern == ExercisePattern.HORIZONTAL_PUSH


async def test_lat_pulldown_without_a_cable_gives_back_exercises(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path)
    home = available_exercises(
        load_catalog(),
        Location.HOME_EQUIPMENT,
        frozenset({Equipment.PULL_UP_BAR, Equipment.RESISTANCE_BANDS}),
        [],
    )
    results = es.find_substitutes(runtime, "machine_lat_pulldown", home)
    catalog = load_catalog()
    assert all(catalog.by_id(i).pattern == ExercisePattern.VERTICAL_PULL for i in _ids(results))  # type: ignore[union-attr]
    assert all(r.close for r in results)


async def test_a_room_without_equipment_gives_bodyweight_moves(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path)
    room = available_exercises(load_catalog(), Location.APARTMENT_NO_EQUIPMENT, frozenset(), [])
    results = es.find_substitutes(runtime, "barbell_back_squat", room)
    catalog = load_catalog()
    assert results
    for exercise_id in _ids(results):
        assert not catalog.by_id(exercise_id).equipment  # type: ignore[union-attr]
    assert results[0].close


async def test_a_restriction_leaves_only_permitted_alternatives(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path)
    knee = [ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes")]
    allowed = available_exercises(
        load_catalog(), Location.PUBLIC_GYM, LOCATION_DEFAULT_EQUIPMENT[Location.PUBLIC_GYM], knee
    )
    results = es.find_substitutes(runtime, "barbell_back_squat", allowed, limit=10)
    catalog = load_catalog()
    for exercise_id in _ids(results):
        exercise = catalog.by_id(exercise_id)
        assert exercise is not None
        assert ScreeningFlag.KNEE_INJURY_CURRENT not in exercise.contraindicated_by


async def test_substitutes_honor_exclusions_and_avoided_areas(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path)
    results = es.find_substitutes(
        runtime,
        "barbell_bent_over_row",
        _gym(),
        excluded=["machine_seated_row"],
        avoid_areas=["lower_back"],
        limit=10,
    )
    catalog = load_catalog()
    assert results and "machine_seated_row" not in _ids(results)
    assert "barbell_bent_over_row" not in _ids(results)
    for exercise_id in _ids(results):
        assert "lower_back" not in catalog.by_id(exercise_id).loads_areas  # type: ignore[union-attr]


async def test_substitutes_work_without_embeddings(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    assert len(runtime.vectors) == 0
    results = es.find_substitutes(runtime, "barbell_bench_press", _gym())
    assert results and results[0].close


def test_an_unknown_exercise_is_an_error() -> None:
    catalog = load_catalog()
    runtime = es.SearchRuntime(catalog, es.VectorIndex.from_records("m", [], valid_hashes={})[0])
    with pytest.raises(es.UnknownExerciseError):
        es.find_substitutes(runtime, "no_such_exercise", _gym())


# --- plan candidate pool --------------------------------------------------------------------


async def test_pool_covers_every_pattern_with_varied_equipment(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    allowed = _gym()
    pool = await es.plan_candidates(db, runtime, allowed)
    catalog = load_catalog()
    assert len(pool) <= es.POOL_SIZE
    assert set(pool) <= {e.id for e in allowed}
    by_pattern: dict[ExercisePattern, list[str]] = {}
    for exercise_id in pool:
        by_pattern.setdefault(catalog.by_id(exercise_id).pattern, []).append(exercise_id)  # type: ignore[union-attr]
    assert set(by_pattern) == {e.pattern for e in allowed}
    pushes = by_pattern[ExercisePattern.HORIZONTAL_PUSH]
    equipment = {tuple(catalog.by_id(i).equipment) for i in pushes}  # type: ignore[union-attr]
    assert len(equipment) == len(pushes)  # no two pushes on the same setup


async def test_pool_keeps_history_and_follows_the_request(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    pool = await es.plan_candidates(
        db,
        runtime,
        _gym(),
        history_ids=["machine_hip_adduction", "not_allowed_anymore"],
        request="biceps curls",
        preferences=[Preference.WEIGHT_TRAINING],
    )
    assert "machine_hip_adduction" in pool and "not_allowed_anymore" not in pool
    assert {"dumbbell_biceps_curl", "barbell_biceps_curl", "cable_biceps_curl"} & set(pool)


async def test_a_small_allowed_set_is_sent_whole(db: Database, tmp_path: Path) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    few = _gym()[: es.POOL_SIZE]
    assert await es.plan_candidates(db, runtime, few) == [e.id for e in few]


def test_exercise_facts_describe_each_id_without_safety_fields() -> None:
    facts = es.exercise_facts(load_catalog(), ["dumbbell_bench_press", "nope"], "ru")
    assert facts == [
        {
            "id": "dumbbell_bench_press",
            "name": "Жим гантелей лёжа",
            "kind": "compound",
            "pattern": "horizontal_push",
            "equipment": ["dumbbells", "bench"],
            "load_unit": "per_implement",
            "kg_loadable": True,
            "qualities": ["strength", "hypertrophy"],
            "measures": ["reps"],
            "intensity": "effort",
        }
    ]
    # ADR 0011: a power exercise is regulated by speed, a carry as a carry; a narrow drill
    # says so.
    by_id = {
        fact["id"]: fact
        for fact in es.exercise_facts(
            load_catalog(),
            ["medicine_ball_chest_pass", "dumbbell_farmer_carry", "bodyweight_shadow_boxing_hook"],
            "en",
        )
    }
    assert by_id["medicine_ball_chest_pass"]["intensity"] == "speed"
    assert by_id["dumbbell_farmer_carry"]["intensity"] == "carry"
    assert by_id["bodyweight_shadow_boxing_hook"]["specific"] is True
    assert "specific" not in by_id["medicine_ball_chest_pass"]


def test_home_selectable_equipment_is_what_the_cli_assumes_for_home() -> None:
    # Keeps the CLI's default for `--location home_equipment` honest.
    assert Equipment.MACHINE not in HOME_SELECTABLE_EQUIPMENT


async def test_a_negated_request_does_not_boost_what_it_rules_out(
    db: Database, tmp_path: Path
) -> None:
    runtime = await _runtime(db, tmp_path, vectors=False)
    plain = await es.plan_candidates(db, runtime, _gym())
    for request in ("no squats please", "без приседаний"):
        assert await es.plan_candidates(db, runtime, _gym(), request=request) == plain


# --- ADR 0009: request reading and quality-aware pools --------------------------------------

_BOXING_REQUEST = (
    "Поддерживающая тренировка для бокса. Развиваем силу удара, выносливость, скорость "
    "перемешения. Давай только бой с тенью будем делать после разминки, а не суперсетами"
)


def test_a_negation_drops_only_its_own_clause() -> None:
    positive = es.positive_request(_BOXING_REQUEST)
    assert "бой с тенью" in positive and "после разминки" in positive
    assert "суперсет" not in positive
    assert es.positive_request("без приседаний и выпадов") == ""
    assert es.positive_request("no squats please") == ""


def test_the_boxing_request_asks_for_power_footwork_and_conditioning() -> None:
    qualities = es.requested_qualities(_BOXING_REQUEST)
    assert {
        Quality.POWER,
        Quality.ROTATIONAL_POWER,
        Quality.AGILITY,
        Quality.CONDITIONING,
    } <= set(qualities)
    assert es.requested_qualities("без прыжков") == []
    assert es.requested_qualities(None, sport=Sport.BOXING_MARTIAL_ARTS)[0] == Quality.POWER
    assert es.requested_qualities(None, focus=Focus.MUSCLE) == [Quality.HYPERTROPHY]


async def test_a_boxing_pool_offers_drills_that_train_each_requested_quality(
    db: Database, tmp_path: Path
) -> None:
    """The observed failure: a 40-exercise pool with no shadow boxing, no jump rope and no
    throws, because the request's "а не суперсетами" switched request matching off."""
    runtime = await _runtime(db, tmp_path, vectors=False)
    catalog = load_catalog()
    qualities = es.requested_qualities(_BOXING_REQUEST, sport=Sport.BOXING_MARTIAL_ARTS)
    pool = await es.plan_candidates(
        db, runtime, _gym(), request=_BOXING_REQUEST, qualities=qualities
    )
    assert "bodyweight_shadow_boxing" in pool
    for quality in qualities:
        trained = [i for i in pool if quality in catalog.by_id(i).qualities]  # type: ignore[union-attr]
        assert trained, quality
    assert any(
        Quality.ROTATIONAL_POWER in catalog.by_id(i).qualities  # type: ignore[union-attr]
        for i in pool
    )
