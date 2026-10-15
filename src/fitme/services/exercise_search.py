"""Hybrid exercise retrieval over the catalog (ADR 0007): search, substitutes, plan pools.

Every function here ranks a set of **candidates the caller has already filtered** with
`services.catalog.available_exercises` (location, equipment, screening contraindications).
Retrieval only orders and trims that set: it can never add an exercise the filter removed,
so no ranking score can override a hard constraint, and safety is never inferred from text
or vector similarity. The guards still judge every plan afterwards.

Signals, each optional:

- **exact** — the query equals an id, a name or a search term (case-insensitive). Always
  ranked first.
- **full-text** — SQLite FTS5 over names and the search document (`catalog_search_fts`).
  Russian has no stemmer there, so a Cyrillic word loses its ending and is matched as a
  prefix ("гантелей" -> "гантел*", "спины" -> "спин*"); a Latin word drops a plural "s"
  ("curls" -> "curl*"). Filler words ("exercise", "without", "упражнение", "без") are
  ignored: lexical matching can't read a negation.
- **name prefix** — a name that starts with the query's words, shortest first, so the plain
  movement outranks its variants ("жим гантелей" -> "Жим гантелей лёжа").
- **pattern words** — a body-region or muscle word ("back", "грудь", "biceps") selects the
  movement patterns it belongs to (`_PATTERN_WORDS`), ranked compound first.
- **semantic** — cosine similarity to the query's embedding, only when
  `FITME_EMBEDDINGS_QUERY` is on and the shipped index has vectors for the current model.
  Any provider error falls back to the other signals; semantic search is never required.

"Avoid this area" is never a text match: `ExerciseQuery.avoid_areas` and
`find_substitutes(avoid_areas=...)` drop every exercise whose catalog `loads_areas` includes
it, before ranking.

The ranked lists are fused with Reciprocal Rank Fusion (`1 / (60 + rank)` per
list, summed), a standard rank-only method that needs no score calibration. Every result
carries `match_reasons` saying which signals found it.

Substitutes rank by catalog structure first — same movement pattern, then similar
mechanics (kind), then similarity of name, loaded areas and (when available) the shipped
exercise-to-exercise embedding similarity — so mechanical equivalence comes from the
catalog, and embeddings only break ties among structurally similar options. No working
weight is carried over: a swapped-in exercise gets the load engine's value (calibration
without history), exactly as today.
"""

from __future__ import annotations

import logging
import re
import weakref
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from fitme.config.embeddings import EmbeddingSettings
from fitme.db.connection import Database
from fitme.db.selectors.catalog_search import FtsQueryError, all_embeddings, fts_match
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import ExerciseKind, ExercisePattern, Focus, Preference
from fitme.llm.embeddings import Embedder, EmbeddingError, OpenAICompatibleEmbedder, embed_query
from fitme.services.catalog_index import documents
from fitme.services.vector_index import VectorError, VectorIndex, normalize

_logger = logging.getLogger(__name__)

RRF_K = 60
_FTS_LIMIT = 200
_SEMANTIC_LIMIT = 50
_MAX_QUERY_LENGTH = 200
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[а-яё]")
_NEGATION_RE = re.compile(
    r"\b(no|not|without|except|avoid|skip|instead|без|не|кроме|никаких|вместо)\b", re.UNICODE
)
# Words that say nothing about which exercise is meant; matching them only adds noise.
_STOP_WORDS = frozenset(
    {
        "exercise", "exercises", "workout", "without", "with", "for", "and", "the", "some",
        "something", "similar", "instead", "load", "loading", "please",
        "упражнение", "упражнения", "упражнений", "тренировка", "без", "для", "что",
        "нибудь", "похожее", "вместо", "нагрузки", "нагрузка", "нагрузкой", "пожалуйста",
    }
)  # fmt: skip


class UnknownExerciseError(LookupError):
    """An exercise id that isn't in the catalog."""


class ExerciseSearchResult(BaseModel):
    exercise_id: str
    score: float
    match_reasons: list[str]
    # Substitutes only: same movement pattern and comparable mechanics.
    close: bool = False


@dataclass(frozen=True, slots=True)
class ExerciseQuery:
    text: str | None = None
    patterns: frozenset[ExercisePattern] = frozenset()
    kinds: frozenset[ExerciseKind] = frozenset()
    excluded: frozenset[str] = frozenset()
    # Catalog `loads_areas` to stay away from ("lower_back", "knee", ...): any exercise that
    # loads one is dropped, a hard filter like `excluded`.
    avoid_areas: frozenset[str] = frozenset()
    limit: int = 20


# --- Runtime --------------------------------------------------------------------------------


@dataclass(slots=True)
class SearchRuntime:
    """What search needs beyond the database: the catalog, the exercise vectors imported at
    startup, and the query embedder when runtime query embedding is on."""

    catalog: Catalog
    vectors: VectorIndex
    embedder: Embedder | None = None
    skipped_vectors: list[str] = field(default_factory=list)


_RUNTIMES: weakref.WeakKeyDictionary[Database, SearchRuntime] = weakref.WeakKeyDictionary()


async def load_runtime(
    db: Database,
    catalog: Catalog,
    embeddings: EmbeddingSettings | None = None,
    embedder: Embedder | None = None,
) -> SearchRuntime:
    """Read the imported vectors once and cache them for this database. `embedder` overrides
    the one `embeddings` would build (tests)."""
    if embeddings is not None:
        settings = embeddings
    else:
        try:
            settings = EmbeddingSettings()
        except ValidationError as exc:
            _logger.warning("search: invalid FITME_EMBEDDINGS_* settings (%s); semantic off", exc)
            settings = EmbeddingSettings.model_construct(
                model="", base_url="", api_key=None, query=False, timeout_s=30.0
            )
    if embedder is None and settings.query_enabled:
        embedder = OpenAICompatibleEmbedder(settings)
    async with db.read() as conn:
        records = await all_embeddings(conn)
    valid = {exercise_id: digest for exercise_id, (_doc, digest) in documents(catalog).items()}
    vectors, skipped = VectorIndex.from_records(settings.model, records, valid_hashes=valid)
    if skipped:
        _logger.warning("search: %d embeddings not used (stale or another model)", len(skipped))
    runtime = SearchRuntime(catalog, vectors, embedder, skipped)
    _RUNTIMES[db] = runtime
    return runtime


async def runtime_for(db: Database, catalog: Catalog) -> SearchRuntime:
    """The cached runtime for `db`, loading it on first use."""
    cached = _RUNTIMES.get(db)
    if cached is not None and cached.catalog is catalog:
        return cached
    return await load_runtime(db, catalog)


def forget_runtime(db: Database) -> None:
    _RUNTIMES.pop(db, None)


# --- Lexical --------------------------------------------------------------------------------


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е").strip()


def _stem(token: str) -> str:
    """A crude, predictable stem for prefix matching: a Russian word loses its ending (two
    letters from 7 letters up, one from 4-6), an English plural its "s"."""
    if _CYRILLIC_RE.search(token):
        if len(token) >= 7:
            return token[:-2]
        if len(token) >= 4:
            return token[:-1]
        return token
    if token.isascii() and len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _query_stems(text: str) -> list[str]:
    stems: list[str] = []
    for token in _TOKEN_RE.findall(_fold(text)):
        if (len(token) < 3 and not token.isdigit()) or token in _STOP_WORDS:
            continue
        stem = _stem(token)
        if stem not in stems:
            stems.append(stem)
    return stems


def fts_expression(text: str) -> str | None:
    """An FTS5 MATCH expression for free text: every word's stem as a quoted prefix term,
    OR-ed. `None` when the text has no usable word."""
    stems = _query_stems(text)
    return " OR ".join(f'"{stem}"*' for stem in stems) if stems else None


# Body-region and muscle words that point at a movement pattern ("back" -> the two pulls,
# "грудь" -> horizontal push). Region words only: a movement word such as "тяга" or "press"
# names many patterns and is left to full-text and semantic matching.
_PATTERN_WORDS: dict[ExercisePattern, frozenset[str]] = {
    ExercisePattern(code): frozenset(words.split())
    for code, words in {
        "squat": "legs quads quadriceps thighs ноги квадрицепс квадрицепсы бедра",
        "hinge": "glutes hamstrings posterior ягодицы ягодичные",
        "horizontal_push": "chest pecs грудь грудные",
        "horizontal_pull": "back lats спина спины широчайшие",
        "vertical_push": "shoulders delts плечи дельты",
        "vertical_pull": "back lats спина спины широчайшие",
        "core": "core abs abdominals trunk пресс кор корпус",
        "mobility": "mobility stretching stretch мобильность растяжка",
        "conditioning": "cardio conditioning кардио выносливость",
        "accessory": "arms biceps triceps calves forearms руки бицепс трицепс икры предплечья",
    }.items()
}


def pattern_matches(text: str) -> list[ExercisePattern]:
    """Patterns whose words the query names ("back" -> the two pulls, "грудь" -> push)."""
    stems = [stem for stem in _query_stems(text) if len(stem) >= 3]
    return [
        pattern
        for pattern in ExercisePattern
        if any(word.startswith(stem) for stem in stems for word in _PATTERN_WORDS[pattern])
    ]


def exact_matches(exercises: Iterable[Exercise], text: str) -> list[str]:
    needle = _fold(text)
    found: list[str] = []
    for exercise in exercises:
        keys = {exercise.id, exercise.id.replace("_", " ")}
        keys.update(exercise.names.values())
        keys.update(exercise.search.values())
        if needle in {_fold(key) for key in keys}:
            found.append(exercise.id)
    return found


def name_prefix_matches(exercises: Iterable[Exercise], text: str) -> list[str]:
    """Exercises with a name that starts with the query's word stems, in order ("жим
    гантелей" -> "Жим гантелей лёжа", not "Кубинский жим гантелей"); shortest name first,
    so the plain movement comes before its variants."""
    stems = _query_stems(text)
    if not stems:
        return []
    hits: list[tuple[int, str]] = []
    for exercise in exercises:
        for name in (*exercise.names.values(), *exercise.search.values()):
            words = _TOKEN_RE.findall(_fold(name))
            if len(words) >= len(stems) and all(
                word.startswith(stem) for word, stem in zip(words, stems, strict=False)
            ):
                hits.append((len(words), exercise.id))
                break
    return [exercise_id for _length, exercise_id in sorted(hits)]


async def _fts_ranked(db: Database, text: str) -> list[str]:
    expression = fts_expression(text)
    if expression is None:
        return []
    try:
        async with db.read() as conn:
            rows = await fts_match(conn, expression, _FTS_LIMIT)
    except FtsQueryError:
        _logger.warning("search: full-text query failed; skipping it")
        return []
    return [exercise_id for exercise_id, _score in rows]


async def _semantic_ranked(
    runtime: SearchRuntime, text: str, allowed: set[str]
) -> list[tuple[str, float]]:
    if runtime.embedder is None or not len(runtime.vectors):
        return []
    if runtime.embedder.model != runtime.vectors.model:
        return []
    try:
        vector = normalize(await embed_query(runtime.embedder, text))
        scores = runtime.vectors.similarities(vector)
    except (EmbeddingError, VectorError) as exc:
        _logger.warning("search: semantic search unavailable (%s); using lexical only", exc)
        return []
    ranked = sorted(
        ((exercise_id, score) for exercise_id, score in scores.items() if exercise_id in allowed),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return ranked[:_SEMANTIC_LIMIT]


# --- Search ---------------------------------------------------------------------------------


def _structural_order(exercise: Exercise) -> tuple[int, int, str]:
    patterns = list(ExercisePattern)
    kind_rank = 0 if exercise.kind == ExerciseKind.COMPOUND else 1
    return (patterns.index(exercise.pattern), kind_rank, exercise.id)


async def search(
    db: Database,
    runtime: SearchRuntime,
    candidates: Sequence[Exercise],
    query: ExerciseQuery,
) -> list[ExerciseSearchResult]:
    """Rank `candidates` (already hard-filtered by the caller) for `query`. With text,
    only exercises some signal matched are returned; without text, every candidate passing
    the structural filters, in catalog-structure order."""
    pool = [
        exercise
        for exercise in candidates
        if exercise.id not in query.excluded
        and not query.avoid_areas.intersection(exercise.loads_areas)
        and (not query.patterns or exercise.pattern in query.patterns)
        and (not query.kinds or exercise.kind in query.kinds)
    ]
    allowed = {exercise.id for exercise in pool}
    limit = max(1, query.limit)
    text = (query.text or "").strip()[:_MAX_QUERY_LENGTH]
    if not text:
        ordered = sorted(pool, key=_structural_order)
        return [
            ExerciseSearchResult(
                exercise_id=exercise.id,
                score=0.0,
                match_reasons=[f"pattern: {exercise.pattern.value}"],
            )
            for exercise in ordered[:limit]
        ]

    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    def add(exercise_id: str, score: float, reason: str) -> None:
        scores[exercise_id] = scores.get(exercise_id, 0.0) + score
        reasons.setdefault(exercise_id, []).append(reason)

    for exercise_id in exact_matches(pool, text):
        add(exercise_id, 1.0, "exact name match")
    for rank, exercise_id in enumerate(name_prefix_matches(pool, text), start=1):
        add(exercise_id, 1.0 / (RRF_K + rank), f"name starts with the query (rank {rank})")
    fts = [exercise_id for exercise_id in await _fts_ranked(db, text) if exercise_id in allowed]
    for rank, exercise_id in enumerate(fts, start=1):
        add(exercise_id, 1.0 / (RRF_K + rank), f"full-text rank {rank}")
    named = pattern_matches(text)
    structural = sorted(
        (exercise for exercise in pool if exercise.pattern in named), key=_structural_order
    )
    for rank, exercise in enumerate(structural, start=1):
        add(exercise.id, 1.0 / (RRF_K + rank), f"pattern named: {exercise.pattern.value}")
    for rank, (exercise_id, similarity) in enumerate(
        await _semantic_ranked(runtime, text, allowed), start=1
    ):
        add(exercise_id, 1.0 / (RRF_K + rank), f"semantic similarity {similarity:.2f}")
    ranked = sorted(scores, key=lambda exercise_id: (-scores[exercise_id], exercise_id))
    return [
        ExerciseSearchResult(
            exercise_id=exercise_id,
            score=round(scores[exercise_id], 6),
            match_reasons=reasons[exercise_id],
        )
        for exercise_id in ranked[:limit]
    ]


# --- Substitutes ----------------------------------------------------------------------------

# Implement words dropped from names before comparing them: "barbell bench press" and
# "dumbbell bench press" are the same movement name. "bench" stays: it names the movement.
_IMPLEMENT_WORDS = frozenset(
    {"barbell", "dumbbell", "kettlebell", "band", "resistance", "cable", "machine", "bodyweight"}
)


def _name_tokens(exercise: Exercise) -> set[str]:
    words = set(_TOKEN_RE.findall(_fold(exercise.names.get("en", exercise.id))))
    return {word.rstrip("s") for word in words if word.rstrip("s") not in _IMPLEMENT_WORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def _kind_score(original: Exercise, other: Exercise) -> int:
    if original.kind == other.kind:
        return 2
    multi_joint = {ExerciseKind.COMPOUND, ExerciseKind.BODYWEIGHT}
    return 1 if {original.kind, other.kind} <= multi_joint else 0


def find_substitutes(
    runtime: SearchRuntime,
    exercise_id: str,
    candidates: Sequence[Exercise],
    *,
    excluded: Iterable[str] = (),
    avoid_areas: Iterable[str] = (),
    limit: int = 5,
) -> list[ExerciseSearchResult]:
    """Allowed replacements for `exercise_id`, closest first. `candidates` must already be
    the user's allowed set (`available_exercises`); the original and `excluded` are never
    returned. Close substitutes (same pattern, comparable mechanics) come first, then
    general alternatives from other patterns. `avoid_areas` drops every candidate loading
    one of those catalog areas (a hard filter, like `excluded`)."""
    original = runtime.catalog.by_id(exercise_id)
    if original is None:
        raise UnknownExerciseError(exercise_id)
    skip = {exercise_id, *excluded}
    avoid = frozenset(avoid_areas)
    neighbors = runtime.vectors.neighbors(exercise_id)
    name = _name_tokens(original)
    areas = set(original.loads_areas)
    scored: list[tuple[tuple[int, int, float, str], ExerciseSearchResult]] = []
    for other in candidates:
        if other.id in skip or avoid.intersection(other.loads_areas):
            continue
        same_pattern = other.pattern == original.pattern
        kind = _kind_score(original, other)
        name_sim = _jaccard(name, _name_tokens(other))
        area_sim = _jaccard(areas, set(other.loads_areas))
        semantic = neighbors.get(other.id)
        similarity = 0.4 * name_sim + 0.3 * area_sim + 0.3 * (semantic or 0.0)
        reasons = [f"pattern: {other.pattern.value}" + (" (same)" if same_pattern else "")]
        reasons.append(f"kind: {other.kind.value}" + (" (same)" if kind == 2 else ""))
        if name_sim:
            reasons.append(f"name overlap {name_sim:.2f}")
        if area_sim:
            reasons.append(f"loaded areas overlap {area_sim:.2f}")
        if semantic is not None:
            reasons.append(f"semantic similarity {semantic:.2f}")
        result = ExerciseSearchResult(
            exercise_id=other.id,
            score=round(int(same_pattern) * 10 + kind * 2 + similarity, 6),
            match_reasons=reasons,
            close=same_pattern and kind >= 1,
        )
        scored.append(((-int(same_pattern), -kind, -similarity, other.id), result))
    scored.sort(key=lambda pair: pair[0])
    return [result for _key, result in scored[: max(1, limit)]]


# --- Plan candidate pool --------------------------------------------------------------------

# An allowed set this small is sent whole. A larger one is narrowed to the per-pattern quotas
# below (40 in total; up to about 50 when the request names patterns, plus exercises with
# history).
POOL_SIZE = 40
_BASE_QUOTA: dict[ExercisePattern, int] = {
    ExercisePattern.SQUAT: 4,
    ExercisePattern.HINGE: 4,
    ExercisePattern.HORIZONTAL_PUSH: 4,
    ExercisePattern.HORIZONTAL_PULL: 4,
    ExercisePattern.VERTICAL_PUSH: 4,
    ExercisePattern.VERTICAL_PULL: 4,
    ExercisePattern.CORE: 4,
    ExercisePattern.MOBILITY: 3,
    ExercisePattern.CONDITIONING: 3,
    ExercisePattern.ACCESSORY: 6,
}


def _quotas(preferences: Sequence[Preference], focus: Focus) -> dict[ExercisePattern, int]:
    quotas = dict(_BASE_QUOTA)
    if Preference.MOBILITY in preferences:
        quotas[ExercisePattern.MOBILITY] += 2
    if Preference.CONDITIONING in preferences or focus == Focus.CONDITIONING:
        quotas[ExercisePattern.CONDITIONING] += 2
    if focus == Focus.MUSCLE or Preference.SPLIT in preferences:
        quotas[ExercisePattern.ACCESSORY] += 3
    return quotas


def _preference_rank(exercise: Exercise, preferences: Sequence[Preference]) -> int:
    if Preference.BODYWEIGHT in preferences:
        return 0 if not exercise.equipment else 1
    if Preference.WEIGHT_TRAINING in preferences:
        return 0 if exercise.kg_loadable else 1
    return 0


def _diverse(ordered: list[Exercise]) -> list[Exercise]:
    """Round-robin over equipment setups so a pattern's first picks aren't all one tool."""
    groups: dict[tuple[str, ...], list[Exercise]] = {}
    for exercise in ordered:
        groups.setdefault(tuple(item.value for item in exercise.equipment), []).append(exercise)
    result: list[Exercise] = []
    queues = list(groups.values())
    while any(queues):
        for queue in queues:
            if queue:
                result.append(queue.pop(0))
    return result


async def plan_candidates(
    db: Database,
    runtime: SearchRuntime,
    allowed: Sequence[Exercise],
    *,
    history_ids: Iterable[str] = (),
    preferences: Sequence[Preference] = (),
    focus: Focus = Focus.GENERAL_FITNESS,
    request: str | None = None,
    size: int = POOL_SIZE,
) -> list[str]:
    """A balanced pool of allowed exercise ids for `plan_generate`: every movement pattern
    the allowed set offers, compound before isolation, varied equipment, exercises the
    request text matches first, and every allowed exercise with history (so progression
    continues). When the allowed set is no larger than `size`, it is returned whole; otherwise
    the pool is the per-pattern quotas plus request matches up to `size`."""
    if len(allowed) <= size:
        return [exercise.id for exercise in allowed]
    allowed_ids = {exercise.id for exercise in allowed}
    relevance: dict[str, int] = {}
    # A negated request ("no squats", "без приседаний") must not boost what it rules out;
    # lexical matching can't read a negation, so such a request leaves the pool structural.
    # The model still reads the request itself.
    if request and request.strip() and not _NEGATION_RE.search(_fold(request)):
        hits = await search(db, runtime, allowed, ExerciseQuery(text=request, limit=size))
        relevance = {hit.exercise_id: rank for rank, hit in enumerate(hits)}
    chosen: list[str] = [exercise_id for exercise_id in history_ids if exercise_id in allowed_ids]
    chosen = list(dict.fromkeys(chosen))
    quotas = _quotas(preferences, focus)
    by_pattern: dict[ExercisePattern, list[Exercise]] = {}
    for exercise in allowed:
        by_pattern.setdefault(exercise.pattern, []).append(exercise)
    # A pattern the request is about gets more room in the pool.
    boosted = {
        hit.pattern
        for exercise_id in sorted(relevance, key=relevance.__getitem__)[:10]
        if (hit := runtime.catalog.by_id(exercise_id)) is not None
    }
    for pattern in boosted:
        quotas[pattern] += 2
    for pattern in ExercisePattern:
        members = sorted(
            by_pattern.get(pattern, []),
            key=lambda exercise: (
                0 if exercise.id in relevance else 1,
                _preference_rank(exercise, preferences),
                0 if exercise.kind == ExerciseKind.COMPOUND else 1,
                relevance.get(exercise.id, 0),
                exercise.id,
            ),
        )
        taken = sum(1 for exercise in members if exercise.id in chosen)
        for exercise in _diverse(members):
            if taken >= quotas.get(pattern, 3):
                break
            if exercise.id not in chosen:
                chosen.append(exercise.id)
                taken += 1
    for exercise_id in sorted(relevance, key=relevance.__getitem__):
        if len(chosen) >= size:
            break
        if exercise_id not in chosen:
            chosen.append(exercise_id)
    order = {exercise.id: index for index, exercise in enumerate(allowed)}
    return sorted(chosen, key=order.__getitem__)


def exercise_facts(
    catalog: Catalog, exercise_ids: Iterable[str], language: str
) -> list[dict[str, object]]:
    """Compact catalog facts the planner sees for each candidate: no instructions, no
    contraindications (those are enforced, not described)."""
    facts: list[dict[str, object]] = []
    for exercise_id in exercise_ids:
        exercise = catalog.by_id(exercise_id)
        if exercise is None:
            continue
        facts.append(
            {
                "id": exercise.id,
                "name": exercise.names.get(language) or exercise.names.get("en", exercise.id),
                "kind": exercise.kind.value,
                "pattern": exercise.pattern.value,
                "equipment": [item.value for item in exercise.equipment],
                "load_unit": exercise.load_unit,
                "kg_loadable": exercise.kg_loadable,
            }
        )
    return facts
