"""`fitme history import` (IMPLEMENTATION_PLAN M11): past trainings and plans from one file
into the training log. The file format is documented in `docs/import-format.md`; this module
is the parser, the validator and the one transaction that writes the result.

What an import means for the guards (A§7, AGENTS.md §2): imported loads are real history.
They set the historical max the ceiling is checked against and the current working load the
engine progresses from — each imported set is written with **planned = actual** (the planned
load is the logged kg, the planned rep range is `reps`–`reps`), so the engine reads the
imported load as the last prescription and holds or adds one increment from there. The import
itself is not a load increase: its decision carries `load_changes = []` (A§4.3 "load changes
count once, when applied": `history_import` = none), so `increases_7d` and the applied-lift
reference are untouched, and a plan confirmed right after an import can still apply its one
weekly increment.

Nothing is guessed. Exercise names resolve only through the file's own `[aliases]` table (or
are catalog ids already); an unknown name is reported, and every session or plan that uses it
is skipped whole, never trimmed. Set loads must pass `guards.plausibility.check_parsed_load`'s
absolute rules (finite, positive, within the bound for the exercise's `load_unit`, and never
a kg on a non-kg-loadable exercise); a session dated in the future is rejected.

Idempotent: every session gets a content hash (`workout_sessions.import_hash`, migration
0006) over its resolved timestamp and its set rows. A session whose hash the log already has
— or that an earlier session in the same file already produced — is reported as a duplicate
and not written. The whole run is one `db.transaction()` (all or nothing); `--dry-run` runs
the very same code and rolls the transaction back at the end, so the report it prints is
exactly what a real run would do, and nothing is written.

Imported sessions attach to one archived "Imported history" plan (`plan_versions.origin =
'import'`, one empty workout with `key = "import"`), because `workout_sessions.
plan_version_id` is NOT NULL and imported trainings did not come from a stored plan. It is
created on the first import and found again through the `history_import` decision outcome
that names it (`selectors.plans.get_history_import_holder_version`). Such sessions have no
`start` decision, so `services.recap.pending_recap_session` never shows a recap for them,
and being `completed` they are never the session `/train` resumes.

Imported `[[plan]]`s are validated with `guards.plan.validate_plan` against a `GuardContext`
built **after** the file's sessions are written (inside the same transaction), so the imported
history is the reference. Like a pasted plan (M8b), a kg on an exercise with no history
becomes `calibration` with the number kept as `declared_kg`. A plan that fails is reported
and skipped; the accepted ones become `plans` + `plan_versions(origin = 'import')`.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from fitme import clock, i18n
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Connection, Database
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import insert_imported_workout_session, insert_set_log
from fitme.db.selectors.plans import (
    get_history_import_holder_version,
    list_plan_version_bodies,
    list_plans_for_user,
)
from fitme.db.selectors.training import last_app_logged_completed_at, list_import_hashes
from fitme.db.selectors.users import get_the_user
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import DecisionKind
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    Prescription,
    ScheduledDay,
    Workout,
    plausible_declared_kg,
)
from fitme.guards.plan import validate_plan
from fitme.guards.plausibility import check_parsed_load
from fitme.services import planning

FORMAT_VERSION = 1
SOURCE_IMPORT = "import"  # `set_logs.source`
HOLDER_WORKOUT_KEY = "import"  # the one workout of the "Imported history" plan
_ORIGIN_IMPORT = "import"
_PLAN_STATUS_ACTIVE = "active"
_PLAN_STATUS_ARCHIVED = "archived"
_DEFAULT_REST_SECONDS = 90
_MAX_REPS = 200
_MAX_SET_INDEX = 100
_NOON = time(12, 0)
_MAX_REPORTED_NAMES = 30  # like M8b's `UNMATCHED_MAX_COUNT`: a display cap, not a limit
_MAX_NAME_LENGTH = 60
_IMPORT_CALIBRATION_RULE = "loads.import_calibration"  # the same rule name M8b logs

_TOP_LEVEL_KEYS = frozenset({"meta", "aliases", "plan", "session"})
_META_KEYS = frozenset({"version", "timezone"})
_SESSION_KEYS = frozenset({"date", "title", "set"})
_SET_KEYS = frozenset({"exercise", "kg", "reps", "set_index", "skipped"})
_PLAN_KEYS = frozenset({"name", "schedule", "workout"})
_SCHEDULE_KEYS = frozenset({"weekday", "workout"})
_WORKOUT_KEYS = frozenset({"key", "title", "exercise"})
_EXERCISE_KEYS = frozenset({"exercise", "sets", "reps", "kg", "load", "rest_seconds", "note"})
_NON_KG_LOADS = frozenset({"bodyweight", "calibration"})


class ImportFormatError(ValueError):
    """The file as a whole is unusable (malformed TOML/JSON, a missing or wrong `[meta]`, an
    unknown key, a bad alias, an invalid timezone). Nothing is imported."""


class NoUserError(RuntimeError):
    """No user exists yet (`fitme activate` has not run): there is nobody to import for."""


# --- Parsed file ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportedSet:
    exercise_id: str
    set_index: int
    kg: float | None
    reps: int
    skipped: bool


@dataclass(frozen=True, slots=True)
class ImportedSession:
    ref: str  # "session #3 (2026-08-10)": how the report names it
    performed_at: datetime  # timezone-aware, UTC
    sets: tuple[ImportedSet, ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class ImportedPlan:
    ref: str  # 'plan "Old two-day"'
    plan: Plan


@dataclass(frozen=True, slots=True)
class Rejection:
    ref: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"ref": self.ref, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ParsedImport:
    timezone: str
    sessions: tuple[ImportedSession, ...]
    plans: tuple[ImportedPlan, ...]
    rejected: tuple[Rejection, ...]
    unknown_exercises: tuple[str, ...]
    guards_fired: tuple[GuardVerdict, ...]  # the failing plausibility verdicts


def detect_format(text: str, path_hint: str | None) -> str:
    """`"json"` for a `.json` path or text whose first non-blank character is `{`; `"toml"`
    otherwise. Both parse with the standard library."""
    if path_hint is not None and path_hint.lower().endswith(".json"):
        return "json"
    return "json" if text.lstrip().startswith("{") else "toml"


def _load_document(text: str, fmt: str) -> dict[str, Any]:
    try:
        document = json.loads(text) if fmt == "json" else tomllib.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ImportFormatError(f"not valid TOML: {exc}") from exc
    if not isinstance(document, dict):
        raise ImportFormatError("the top level must be a table (an object in JSON)")
    return document


def _check_keys(table: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ImportFormatError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _table(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImportFormatError(f"{where}: expected a table")
    return value


def _table_list(value: object, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ImportFormatError(f"{where}: expected a list of tables")
    return [_table(item, f"{where}[{index + 1}]") for index, item in enumerate(value)]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MAX_NAME_LENGTH else text[: _MAX_NAME_LENGTH - 1] + "…"


# --- Sessions ---------------------------------------------------------------------------------


class _ItemError(ValueError):
    """A problem with one session or one plan: that item is rejected, the file goes on."""


@dataclass(slots=True)
class _Resolver:
    catalog: Catalog
    aliases: Mapping[str, str]
    unknown: list[str] = field(default_factory=list)

    def resolve(self, name: object, where: str) -> Exercise:
        if not isinstance(name, str) or not name.strip():
            raise _ItemError(f"{where}: exercise must be a non-empty name")
        exercise_id = self.aliases.get(name)
        exercise = self.catalog.by_id(exercise_id if exercise_id is not None else name)
        if exercise is None:
            shown = _short(name)
            if shown not in self.unknown:
                self.unknown.append(shown)
            raise _ItemError(f"{where}: unknown exercise {shown!r} (add it to [aliases])")
        return exercise


def _resolve_timestamp(value: object, tz: ZoneInfo, where: str) -> tuple[datetime, str]:
    """The instant a session was performed (UTC) and the short date text used in its `ref`.
    A date-only value is noon in `tz`; a naive datetime is read in `tz`; an aware one is
    used as given. Strings are ISO 8601 (`YYYY-MM-DD` counts as date-only)."""
    resolved: datetime
    if isinstance(value, datetime):
        resolved = value
    elif isinstance(value, date):
        resolved = datetime.combine(value, _NOON, tzinfo=tz)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                resolved = datetime.combine(date.fromisoformat(text), _NOON, tzinfo=tz)
            else:
                resolved = datetime.fromisoformat(text)
        except ValueError as exc:
            raise _ItemError(f"{where}: date {_short(text)!r} is not an ISO date/datetime") from exc
    else:
        raise _ItemError(f"{where}: date must be a date, a datetime or an ISO string")
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=tz)
    resolved = resolved.astimezone(UTC)
    return resolved, resolved.astimezone(tz).date().isoformat()


def _content_hash(performed_at: datetime, sets: Sequence[ImportedSet]) -> str:
    payload = {
        "v": FORMAT_VERSION,
        "at": clock.format_timestamp(performed_at),
        "sets": [
            [item.exercise_id, item.set_index, item.kg, item.reps, item.skipped] for item in sets
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_set(
    raw: dict[str, Any],
    resolver: _Resolver,
    next_index: dict[str, int],
    used_index: set[tuple[str, int]],
    where: str,
    fired: list[GuardVerdict],
) -> ImportedSet:
    _check_keys(raw, _SET_KEYS, where)
    exercise = resolver.resolve(raw.get("exercise"), where)

    kg_raw = raw.get("kg")
    kg: float | None = None
    if kg_raw is not None:
        if not _is_number(kg_raw):
            raise _ItemError(f"{where}: kg must be a number")
        kg = float(kg_raw)
    if exercise.kg_loadable and kg is None:
        raise _ItemError(f"{where}: {exercise.id} takes a kg load; kg is required")
    if not exercise.kg_loadable and kg is not None:
        raise _ItemError(f"{where}: {exercise.id} takes no kg load; leave kg out")
    verdict = check_parsed_load(exercise, Load(kind="calibration"), kg)
    if not verdict.ok:
        fired.append(verdict)
        raise _ItemError(f"{where}: {verdict.detail}")

    reps = raw.get("reps")
    if isinstance(reps, bool) or not isinstance(reps, int) or not 1 <= reps <= _MAX_REPS:
        raise _ItemError(f"{where}: reps must be a whole number from 1 to {_MAX_REPS}")

    skipped = raw.get("skipped", False)
    if not isinstance(skipped, bool):
        raise _ItemError(f"{where}: skipped must be true or false")

    index_raw = raw.get("set_index")
    if index_raw is None:
        set_index = next_index.get(exercise.id, 0) + 1
    else:
        if not _is_int(index_raw) or not 1 <= index_raw <= _MAX_SET_INDEX:
            raise _ItemError(
                f"{where}: set_index must be a whole number from 1 to {_MAX_SET_INDEX}"
            )
        set_index = index_raw
    if (exercise.id, set_index) in used_index:
        raise _ItemError(f"{where}: set_index {set_index} is used twice for {exercise.id}")
    used_index.add((exercise.id, set_index))
    next_index[exercise.id] = max(next_index.get(exercise.id, 0), set_index)
    return ImportedSet(
        exercise_id=exercise.id, set_index=set_index, kg=kg, reps=reps, skipped=skipped
    )


def _parse_session(
    raw: dict[str, Any],
    position: int,
    resolver: _Resolver,
    tz: ZoneInfo,
    now: datetime,
    fired: list[GuardVerdict],
) -> ImportedSession:
    where = f"session #{position}"
    _check_keys(raw, _SESSION_KEYS, where)
    if "date" not in raw:
        raise _ItemError(f"{where}: date is required")
    performed_at, day = _resolve_timestamp(raw["date"], tz, where)
    where = f"session #{position} ({day})"
    if performed_at > now:
        raise _ItemError(f"{where}: dated in the future")
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise _ItemError(f"{where}: title must be a string")
    raw_sets = raw.get("set")
    if not isinstance(raw_sets, list) or not raw_sets:
        raise _ItemError(f"{where}: at least one [[session.set]] is required")

    sets: list[ImportedSet] = []
    next_index: dict[str, int] = {}
    used_index: set[tuple[str, int]] = set()
    problems: list[str] = []
    for set_position, raw_set in enumerate(raw_sets, start=1):
        set_where = f"{where} set {set_position}"
        if not isinstance(raw_set, dict):
            problems.append(f"{set_where}: expected a table")
            continue
        try:
            sets.append(_parse_set(raw_set, resolver, next_index, used_index, set_where, fired))
        except _ItemError as exc:
            problems.append(str(exc))
    if problems:
        raise _ItemError("; ".join(problems))
    return ImportedSession(
        ref=where,
        performed_at=performed_at,
        sets=tuple(sets),
        content_hash=_content_hash(performed_at, sets),
    )


# --- Plans ------------------------------------------------------------------------------------


def _parse_reps(value: object, where: str) -> tuple[int, int]:
    if _is_int(value):
        return int(value), int(value)  # type: ignore[call-overload]  # narrowed by _is_int
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_int(item) for item in value)
        and value[0] <= value[1]
    ):
        return int(value[0]), int(value[1])
    raise _ItemError(f"{where}: reps must be a number or [min, max] with min <= max")


def _parse_prescription(raw: dict[str, Any], resolver: _Resolver, where: str) -> Prescription:
    _check_keys(raw, _EXERCISE_KEYS, where)
    exercise = resolver.resolve(raw.get("exercise"), where)
    reps_min, reps_max = _parse_reps(raw.get("reps"), where)
    kg_raw = raw.get("kg")
    load_raw = raw.get("load")
    if (kg_raw is None) == (load_raw is None):
        raise _ItemError(f'{where}: give either kg or load = "bodyweight" / "calibration"')
    load: Load
    if kg_raw is not None:
        if not _is_number(kg_raw) or not math.isfinite(float(kg_raw)) or float(kg_raw) <= 0:
            raise _ItemError(f"{where}: kg must be a positive number")
        load = Load(kind="kg", kg=float(kg_raw))
    else:
        if load_raw not in _NON_KG_LOADS:
            raise _ItemError(f'{where}: load must be "bodyweight" or "calibration"')
        load = Load(kind=load_raw)
    try:
        return Prescription(
            exercise_id=exercise.id,
            sets=raw.get("sets"),
            reps_min=reps_min,
            reps_max=reps_max,
            load=load,
            rest_seconds=raw.get("rest_seconds", _DEFAULT_REST_SECONDS),
            note=raw.get("note"),
        )
    except ValidationError as exc:
        raise _ItemError(f"{where}: {_validation_summary(exc)}") from exc


def _validation_summary(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "value"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _parse_plan(raw: dict[str, Any], position: int, resolver: _Resolver) -> ImportedPlan:
    where = f"plan #{position}"
    _check_keys(raw, _PLAN_KEYS, where)
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _ItemError(f"{where}: name is required")
    where = f"plan {_short(name)!r}"

    schedule: list[ScheduledDay] = []
    for index, day in enumerate(raw.get("schedule", []) or [], start=1):
        day_where = f"{where} schedule[{index}]"
        if not isinstance(day, dict):
            raise _ItemError(f"{day_where}: expected a table")
        _check_keys(day, _SCHEDULE_KEYS, day_where)
        try:
            schedule.append(
                ScheduledDay(weekday=day.get("weekday"), workout_key=day.get("workout"))
            )
        except ValidationError as exc:
            raise _ItemError(f"{day_where}: {_validation_summary(exc)}") from exc

    workouts: list[Workout] = []
    raw_workouts = raw.get("workout")
    if not isinstance(raw_workouts, list) or not raw_workouts:
        raise _ItemError(f"{where}: at least one [[plan.workout]] is required")
    for index, raw_workout in enumerate(raw_workouts, start=1):
        workout_where = f"{where} workout[{index}]"
        if not isinstance(raw_workout, dict):
            raise _ItemError(f"{workout_where}: expected a table")
        _check_keys(raw_workout, _WORKOUT_KEYS, workout_where)
        raw_items = raw_workout.get("exercise")
        if not isinstance(raw_items, list) or not raw_items:
            raise _ItemError(f"{workout_where}: at least one [[plan.workout.exercise]] is required")
        blocks: list[Block] = []
        for item_index, raw_item in enumerate(raw_items, start=1):
            item_where = f"{workout_where} exercise[{item_index}]"
            if not isinstance(raw_item, dict):
                raise _ItemError(f"{item_where}: expected a table")
            blocks.append(
                Block(kind="single", items=[_parse_prescription(raw_item, resolver, item_where)])
            )
        try:
            workouts.append(
                Workout(
                    key=raw_workout.get("key"),
                    title=raw_workout.get("title", raw_workout.get("key")),
                    blocks=blocks,
                )
            )
        except ValidationError as exc:
            raise _ItemError(f"{workout_where}: {_validation_summary(exc)}") from exc
    try:
        plan = Plan(name=name, schedule=schedule, workouts=workouts)
    except ValidationError as exc:
        raise _ItemError(f"{where}: {_validation_summary(exc)}") from exc
    return ImportedPlan(ref=where, plan=plan)


# --- The file -----------------------------------------------------------------------------------


def _parse_meta(document: Mapping[str, Any], default_timezone: str | None) -> ZoneInfo:
    if "meta" not in document:
        raise ImportFormatError("[meta] is required (with version = 1)")
    meta = _table(document["meta"], "[meta]")
    _check_keys(meta, _META_KEYS, "[meta]")
    if meta.get("version") != FORMAT_VERSION:
        raise ImportFormatError(f"[meta] version must be {FORMAT_VERSION}")
    timezone = meta.get("timezone", default_timezone)
    if timezone is None:
        return ZoneInfo("UTC")
    if not isinstance(timezone, str):
        raise ImportFormatError("[meta] timezone must be an IANA name")
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ImportFormatError(
            f"[meta] timezone {_short(timezone)!r} is not a known IANA name"
        ) from exc


def _parse_aliases(document: Mapping[str, Any], catalog: Catalog) -> dict[str, str]:
    aliases = _table(document.get("aliases", {}), "[aliases]")
    resolved: dict[str, str] = {}
    for name, target in aliases.items():
        if not isinstance(target, str) or catalog.by_id(target) is None:
            raise ImportFormatError(
                f"[aliases] {_short(name)!r} points at {_short(str(target))!r}, which is not a "
                "catalog exercise id"
            )
        resolved[name] = target
    return resolved


def parse_import(
    text: str,
    *,
    fmt: str,
    catalog: Catalog,
    default_timezone: str | None,
    now: datetime,
) -> ParsedImport:
    """Parse and validate one import file (pure: no I/O). Raises `ImportFormatError` when the
    file as a whole is unusable; per-item problems become `rejected` entries instead."""
    document = _load_document(text, fmt)
    _check_keys(document, _TOP_LEVEL_KEYS, "top level")
    tz = _parse_meta(document, default_timezone)
    resolver = _Resolver(catalog=catalog, aliases=_parse_aliases(document, catalog))

    rejected: list[Rejection] = []
    fired: list[GuardVerdict] = []
    sessions: list[ImportedSession] = []
    for position, raw in enumerate(_table_list(document.get("session", []), "[[session]]"), 1):
        try:
            sessions.append(_parse_session(raw, position, resolver, tz, now, fired))
        except _ItemError as exc:
            rejected.append(Rejection(ref=f"session #{position}", reason=str(exc)))

    plans: list[ImportedPlan] = []
    for position, raw in enumerate(_table_list(document.get("plan", []), "[[plan]]"), 1):
        try:
            plans.append(_parse_plan(raw, position, resolver))
        except _ItemError as exc:
            rejected.append(Rejection(ref=f"plan #{position}", reason=str(exc)))

    return ParsedImport(
        timezone=tz.key,
        sessions=tuple(sessions),
        plans=tuple(plans),
        rejected=tuple(rejected),
        unknown_exercises=tuple(resolver.unknown[:_MAX_REPORTED_NAMES]),
        guards_fired=tuple(fired),
    )


# --- The import -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SavedPlan:
    name: str
    plan_id: int
    plan_version_id: int


@dataclass(frozen=True, slots=True)
class ImportReport:
    dry_run: bool
    file_sha256: str
    timezone: str
    sessions_new: int
    sessions_duplicate: int
    rejected: tuple[Rejection, ...]  # sessions and plans, parse-time and validation-time
    unknown_exercises: tuple[str, ...]
    plans_saved: tuple[SavedPlan, ...]
    plans_duplicate: int  # same body as an already-imported plan: skipped, not re-saved
    session_ids: tuple[int, ...]
    # Per `per_implement` exercise among the new sessions, the highest imported kg — shown
    # as "X kg each", the one convention that is easy to get wrong (a two-dumbbell total).
    per_implement_max: tuple[tuple[str, float], ...]
    # How many of the new sessions are newer than the last training logged through the app
    # (all of them when there is none): those become the current working load the engine
    # holds at, so the report says so.
    newer_than_last_logged: int
    decision_id: int | None  # None on a dry run (rolled back)


class _DryRunRollback(Exception):
    """Raised inside the transaction on a dry run so `db.transaction()` rolls it back; carries
    the report the rolled-back run produced."""

    def __init__(self, report: ImportReport) -> None:
        super().__init__("dry run")
        self.report = report


def _holder_plan(lang: str) -> Plan:
    return Plan(
        name=i18n.t("history_import.plan_name", lang),
        schedule=[],
        workouts=[
            Workout(
                key=HOLDER_WORKOUT_KEY,
                title=i18n.t("history_import.workout_title", lang),
                blocks=[],
            )
        ],
    )


async def _holder_version_id(conn: Connection, user_id: int, lang: str, decision_id: int) -> int:
    existing = await get_history_import_holder_version(conn, user_id)
    if existing is not None:
        return existing.id
    plan_id = await insert_plan(
        conn,
        user_id=user_id,
        name=i18n.t("history_import.plan_name", lang),
        is_default=False,
        status=_PLAN_STATUS_ARCHIVED,
    )
    return await insert_plan_version(
        conn,
        plan_id=plan_id,
        version=1,
        body=_holder_plan(lang).model_dump(mode="json"),
        origin=_ORIGIN_IMPORT,
        decision_id=decision_id,
    )


async def _write_session(
    conn: Connection, user_id: int, holder_version_id: int, session: ImportedSession
) -> int:
    session_id = await insert_imported_workout_session(
        conn,
        user_id=user_id,
        plan_version_id=holder_version_id,
        workout_key=HOLDER_WORKOUT_KEY,
        performed_at=session.performed_at,
        import_hash=session.content_hash,
    )
    for item in session.sets:
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id=item.exercise_id,
            set_index=item.set_index,
            planned_load_kg=item.kg,
            planned_reps_min=item.reps,
            planned_reps_max=item.reps,
            actual_load_kg=None if item.skipped else item.kg,
            actual_reps=None if item.skipped else item.reps,
            skipped=item.skipped,
            rpe=None,
            source=SOURCE_IMPORT,
        )
    return session_id


def _apply_no_history_calibration(plan: Plan, inputs: planning.Inputs) -> list[GuardVerdict]:
    """M8b's rule for a transcribed plan, in place: a kg on a kg-loadable exercise with no
    logged history becomes `calibration`, the number kept as the display hint."""
    fired: list[GuardVerdict] = []
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                exercise = inputs.catalog.by_id(prescription.exercise_id)
                kg = prescription.load.kg
                if exercise is None or kg is None or not exercise.kg_loadable:
                    continue
                if inputs.ctx.history_max_kg.get(exercise.id) is not None:
                    continue
                prescription.declared_kg = plausible_declared_kg(
                    kg, per_implement=exercise.load_unit != "total"
                )
                prescription.load = Load(kind="calibration")
                fired.append(
                    GuardVerdict(
                        rule=_IMPORT_CALIBRATION_RULE,
                        ok=True,
                        detail=(
                            f"{exercise.id}: no logged history, so the declared {kg:g} kg "
                            "becomes a calibration load; the declared value is kept as a "
                            "display hint only"
                        ),
                    )
                )
    return fired


async def _save_plans(
    conn: Connection,
    *,
    user_id: int,
    settings: Settings,
    catalog: Catalog,
    plans: Sequence[ImportedPlan],
    decision_id: int,
) -> tuple[list[SavedPlan], int, list[Rejection], list[GuardVerdict]]:
    """Validate every parsed plan against the history as it is now (the file's sessions
    included) and save the ones that pass. Returns the saved plans, how many were skipped as
    duplicates of an already-imported plan (same body, so a re-import saves no second copy),
    the rejections and every verdict worth logging."""
    saved: list[SavedPlan] = []
    duplicates = 0
    rejected: list[Rejection] = []
    fired: list[GuardVerdict] = []
    if not plans:
        return saved, duplicates, rejected, fired
    snapshot = await planning.read_snapshot(conn, user_id)
    gate_failure = planning.gate(snapshot)
    if gate_failure is not None:
        _code, verdict = gate_failure
        fired.append(verdict)
        for item in plans:
            rejected.append(
                Rejection(ref=item.ref, reason=f"plans need a complete setup: {verdict.detail}")
            )
        return saved, duplicates, rejected, fired
    inputs = planning.build_inputs(catalog, snapshot, settings, user_id)
    has_default = any(
        plan.is_default and plan.status == _PLAN_STATUS_ACTIVE
        for plan in await list_plans_for_user(conn, user_id)
    )
    known_bodies = {
        _canonical(body)
        for body in await list_plan_version_bodies(conn, user_id, origin=_ORIGIN_IMPORT)
    }
    for item in plans:
        plan = item.plan.model_copy(deep=True)
        calibration_fired = _apply_no_history_calibration(plan, inputs)
        body = plan.model_dump(mode="json")
        if _canonical(body) in known_bodies:
            duplicates += 1
            continue
        fired.extend(calibration_fired)
        verdicts = validate_plan(plan, inputs.ctx)
        failures = [verdict for verdict in verdicts if not verdict.ok]
        if failures:
            fired.extend(failures)
            reasons = "; ".join(f"{verdict.rule}: {verdict.detail}" for verdict in failures)
            rejected.append(Rejection(ref=item.ref, reason=reasons))
            continue
        known_bodies.add(_canonical(body))
        plan_id = await insert_plan(
            conn,
            user_id=user_id,
            name=plan.name,
            is_default=not has_default,
            status=_PLAN_STATUS_ACTIVE,
        )
        has_default = True
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body=body,
            origin=_ORIGIN_IMPORT,
            decision_id=decision_id,
        )
        saved.append(SavedPlan(name=plan.name, plan_id=plan_id, plan_version_id=version_id))
    return saved, duplicates, rejected, fired


def _canonical(body: Mapping[str, object]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


async def _run(
    conn: Connection,
    *,
    user_id: int,
    lang: str,
    settings: Settings,
    catalog: Catalog,
    parsed: ParsedImport,
    file_sha256: str,
    source: str,
    dry_run: bool,
) -> ImportReport:
    existing = await list_import_hashes(conn, user_id)
    seen: set[str] = set()
    new_sessions: list[ImportedSession] = []
    duplicates = 0
    for session in parsed.sessions:
        if session.content_hash in existing or session.content_hash in seen:
            duplicates += 1
            continue
        seen.add(session.content_hash)
        new_sessions.append(session)

    per_implement_max: dict[str, float] = {}
    for session in new_sessions:
        for item in session.sets:
            exercise = catalog.by_id(item.exercise_id)
            if item.kg is None or exercise is None or exercise.load_unit != "per_implement":
                continue
            known = per_implement_max.get(item.exercise_id)
            per_implement_max[item.exercise_id] = item.kg if known is None else max(known, item.kg)
    last_logged = await last_app_logged_completed_at(conn, user_id)
    newer_than_last_logged = sum(
        1
        for session in new_sessions
        if last_logged is None or clock.format_timestamp(session.performed_at) > last_logged
    )

    rejected = list(parsed.rejected)
    user_report: dict[str, object] = {
        "source": source,
        "file_sha256": file_sha256,
        "timezone": parsed.timezone,
        "dry_run": dry_run,
        "sessions": {
            "declared": len(parsed.sessions)
            + sum(1 for item in parsed.rejected if item.ref.startswith("session")),
            "new": len(new_sessions),
            "duplicate": duplicates,
            "rejected": sum(1 for item in parsed.rejected if item.ref.startswith("session")),
        },
        "plans": {
            "declared": len(parsed.plans)
            + sum(1 for item in parsed.rejected if item.ref.startswith("plan"))
        },
        "unknown_exercises": list(parsed.unknown_exercises),
        "rejected": [item.as_dict() for item in parsed.rejected],
    }
    proposal: dict[str, object] = {
        "sessions": [
            {
                "at": clock.format_timestamp(session.performed_at),
                "hash": session.content_hash,
                "sets": len(session.sets),
            }
            for session in new_sessions
        ],
        "plans": [item.plan.name for item in parsed.plans],
    }
    decision_id = await insert_decision(
        conn,
        user_id=user_id,
        kind=DecisionKind.HISTORY_IMPORT.value,
        prompt_template=None,
        prompt_version=None,
        model=None,
        content_version=content_version(),
        llm_input=None,
        user_report=user_report,
        proposal=proposal,
        guards_fired=[verdict.model_dump() for verdict in parsed.guards_fired],
        load_changes=[],  # A§4.3: an import is not a load increase
    )

    session_ids: list[int] = []
    holder_version_id: int | None = None
    if new_sessions:
        holder_version_id = await _holder_version_id(conn, user_id, lang, decision_id)
        for session in new_sessions:
            session_ids.append(await _write_session(conn, user_id, holder_version_id, session))

    saved, plan_duplicates, plan_rejections, plan_fired = await _save_plans(
        conn,
        user_id=user_id,
        settings=settings,
        catalog=catalog,
        plans=parsed.plans,
        decision_id=decision_id,
    )
    rejected.extend(plan_rejections)

    outcome: dict[str, object] = {
        "holder_plan_version_id": holder_version_id,
        "session_ids": session_ids,
        "plans_saved": [
            {"name": item.name, "plan_id": item.plan_id, "plan_version_id": item.plan_version_id}
            for item in saved
        ],
        "plans_duplicate": plan_duplicates,
        "plans_rejected": [item.as_dict() for item in plan_rejections],
        "guards_fired": [verdict.model_dump() for verdict in plan_fired],
    }
    await insert_decision_outcome(conn, decision_id=decision_id, outcome=outcome)

    return ImportReport(
        dry_run=dry_run,
        file_sha256=file_sha256,
        timezone=parsed.timezone,
        sessions_new=len(new_sessions),
        sessions_duplicate=duplicates,
        rejected=tuple(rejected),
        unknown_exercises=parsed.unknown_exercises,
        plans_saved=tuple(saved),
        plans_duplicate=plan_duplicates,
        session_ids=tuple(session_ids),
        per_implement_max=tuple(sorted(per_implement_max.items())),
        newer_than_last_logged=newer_than_last_logged,
        decision_id=None if dry_run else decision_id,
    )


async def import_history(
    db: Database,
    settings: Settings,
    text: str,
    *,
    fmt: str,
    source: str,
    dry_run: bool = False,
) -> ImportReport:
    """Import one file's sessions and plans for the one user, in one transaction. `source`
    is a short label for the decision record (`"stdin"` or the file's base name). A dry run
    executes the identical code path and rolls it back: the report is exact and nothing is
    written. Raises `ImportFormatError` for an unusable file and `NoUserError` before
    activation."""
    async with db.read() as conn:
        user = await get_the_user(conn)
    if user is None:
        raise NoUserError("no user yet; run `fitme activate` first")
    catalog = load_catalog()
    parsed = parse_import(
        text, fmt=fmt, catalog=catalog, default_timezone=user.timezone, now=clock.now()
    )
    file_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    try:
        async with db.transaction() as conn:
            report = await _run(
                conn,
                user_id=user.id,
                lang=user.language,
                settings=settings,
                catalog=catalog,
                parsed=parsed,
                file_sha256=file_sha256,
                source=source,
                dry_run=dry_run,
            )
            if dry_run:
                raise _DryRunRollback(report)
    except _DryRunRollback as rollback:
        return rollback.report
    return report
