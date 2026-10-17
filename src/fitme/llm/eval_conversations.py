"""`fitme llm eval-conversations` business logic (experiment: llm-first planning).

A manual, real-money tool, like `llm/eval.py`: never run against a real provider in tests.
It replays the regression scenarios (`tests/fixtures/llm_eval/conversations/*.json`) as
multi-turn conversations through the real service path, against a temp migrated SQLite
database seeded for each run, in two modes:

- **planner**: each turn is what the bot's free-text handler does with a message — save it,
  link it to the active session, the stop-word scan, then `services.assistant.handle_message`
  (outside a started workout that is the conversational `planner`).
- **legacy**: the same requests through the pipeline the planner replaces — the first
  request is `planning.propose_new_plan(guidance=...)`, each follow-up is
  `planning.revise_plan` on the current draft (the base the draft's Change button gives),
  and the save is `planning.confirm_plan`.

Every turn is judged by the fixture's `expect` block: deterministic, data-only checks (ids,
kinds and equipment from the catalog, weekdays, counts, load kinds) — proxies for "did it do
what was asked", never a judgement of the training itself. The report (`render_report`) puts
both modes side by side with model calls, tokens, estimated cost and latency, then the full
transcripts with every staged plan, for a person to read.

Model calls per turn are the `llm_calls` rows the turn wrote (one per agent run; a run may
make several model requests — tool calls — inside it, which are not counted separately).
"""

from __future__ import annotations

import difflib
import json
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database, open_database
from fitme.db.controllers.profile import (
    delete_setup_progress,
    upsert_profile,
    upsert_screening_flag,
)
from fitme.db.controllers.users import insert_user
from fitme.db.migrate import migrate
from fitme.db.search_index_file import SearchIndexFileError
from fitme.db.selectors.decisions import list_llm_calls_since
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.domain.catalog import Catalog
from fitme.domain.enums import (
    Equipment,
    ExerciseKind,
    Location,
    Measure,
    ScreeningFlag,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load, Plan, Prescription, is_activity_id
from fitme.services import (
    assistant,
    catalog_index,
    conversations,
    exercise_search,
    history_import,
    planning,
    training,
)
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.safety import record_incoming_text, scan_and_maybe_halt

Mode = Literal["planner", "legacy"]
MODES: tuple[Mode, ...] = ("planner", "legacy")
CheckOutcome = Literal["pass", "fail", "n/a"]

# Keys a single-turn profile fixture (`tests/fixtures/llm_eval/*.json`) carries that are not
# profile fields; they are dropped when a conversation references that fixture.
_NON_PROFILE_KEYS = frozenset({"name", "user_id", "history", "request", "exclusions"})
# Reported, never counted toward checks passed: the running minutes are a number to read,
# and a question is what the planner is told to end every proposal with, so asking says
# nothing about whether it understood (the next turn's plan checks carry that weight).
_SOFT_CHECKS = frozenset({"cardio_minutes", "question"})
# Load checks read what the model asked for: in planner mode the plan before the guards'
# load substitution; in legacy mode (only the judged plan exists) a load guard that fired on
# the exercise in that turn fails the check.
_LOAD_RULES = frozenset({"progression.weekly_cap", "plan.kg_loadable", "loads.substituted"})
_LOAD_RULE_PREFIXES = ("ceiling.", "checkins.")
LEGACY_NO_LOCATION = "legacy cannot represent a plan location"
LEGACY_NO_ACTIVITY = "legacy cannot represent an activity item"
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

LEGEND = (
    "Model calls = agent runs; a planner run with N tool round-trips counts once; legacy "
    "best-of and escalation attempts count separately. Failed calls are listed apart and "
    "their tokens are not recorded. Questions and the running minutes are reported, not "
    "counted in checks passed. Load checks read the plan as the model staged it (planner) or "
    "fail when a load guard fired on the exercise in that turn (legacy). In legacy mode, "
    f"checks that need a plan location or an activity item are n/a: {LEGACY_NO_LOCATION}, "
    f"and {LEGACY_NO_ACTIVITY}."
)

CLOSING_PARAGRAPH = (
    "The checks above are deterministic proxies for intent: they read ids, kinds and "
    "equipment from the catalog, weekdays, counts and load kinds, and nothing else. They are "
    "not a judgement of training quality, and a pass does not mean the plan is good. A person "
    "must read the transcripts and the staged plans before drawing a conclusion."
)


# --- Fixture schema ---------------------------------------------------------------------------


_FORBID = ConfigDict(extra="forbid")


class ProfileSpec(BaseModel):
    """The profile a conversation runs with (the keys of the single-turn eval fixtures)."""

    model_config = _FORBID

    language: Literal["en", "ru"]
    age_bucket: str
    weight_bucket: str
    experience: str
    barbell_experience: str
    preferences: list[str] = Field(default_factory=list)
    focus: str | None = None
    location: Location
    equipment: list[Equipment] = Field(default_factory=list)
    sessions_per_week: int = Field(ge=1, le=7)
    session_minutes: int = Field(ge=10, le=240)
    flags: list[dict[str, str]] = Field(default_factory=list)
    sport: str | None = None
    sport_sessions_per_week: int | None = None

    @model_validator(mode="after")
    def _known_flags(self) -> Self:
        for entry in self.flags:
            ScreeningFlag(entry["flag"])  # raises on an unknown flag name
            if entry.get("value") not in ("yes", "no"):
                raise ValueError(f"flag {entry['flag']}: value must be yes or no")
        return self


class HistoryEntry(BaseModel):
    """One past session's sets of one exercise, imported through `services.history_import`
    (so the `set_logs` are real history the load engine and the guards read)."""

    model_config = _FORBID

    exercise_id: str
    kg: float | None = Field(default=None, gt=0)
    reps: int = Field(ge=1, le=50)
    sets: int = Field(default=3, ge=1, le=10)
    days_ago: int = Field(default=7, ge=1, le=365)


class SwapLoad(BaseModel):
    """After replacing `from` with `to`: `to`'s load is not kg, or its kg is at most `from`'s
    kg in the previous plan (or its seeded history kg)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_id: str = Field(alias="from")
    to: str


class Expect(BaseModel):
    """Deterministic checks over one turn's outcome. Plan checks read the turn's plan: the
    current draft after the turn, or the plan saved in it."""

    model_config = _FORBID

    question: bool | None = None  # soft: nothing staged, "?" in the reply's last line
    draft: bool | None = None  # an unsaved draft is current after the turn
    saved: bool | None = None  # a saved plan version equals the last staged draft
    days: list[int] | None = None  # the exact set of scheduled weekdays (0 = Monday)
    days_count: int | None = Field(default=None, ge=0, le=7)
    workouts: int | None = Field(default=None, ge=0)
    items_max: int | None = Field(default=None, ge=0)  # items in any one workout
    no_kg_loads: bool | None = None  # load check
    no_equipment: bool | None = None  # every catalog item's equipment is []
    exclude_equipment: list[Equipment] | None = None
    exclude_ids: list[str] | None = None
    exclude_kinds: list[ExerciseKind] | None = None
    include_ids_any: list[str] | None = None
    activity_items_min: int | None = Field(default=None, ge=0)  # over the scheduled days
    reps_items_max: int | None = Field(default=None, ge=0)  # items prescribed in reps
    location: Location | None = None  # the plan's location, else the profile's
    outdoor_allowed: bool | None = None  # every catalog item lists `outdoor` in `locations`
    catalog_only: bool | None = None  # every item is a catalog exercise
    compound_loads: list[Literal["kg", "bodyweight", "calibration"]] | None = None  # load
    history_loads: bool | None = None  # load check: every seeded-history exercise is in kg
    single_change: bool | None = None  # one exercise id out and/or one in, vs the previous plan
    unchanged_except: list[str] | None = None  # only these ids' items may differ from it
    swap_load: SwapLoad | None = None  # load check
    cardio_minutes: tuple[int, int] | None = None  # soft: longest workout's timed cardio
    any_of: list[Expect] | None = None  # passes when every check of one alternative passes

    @model_validator(mode="after")
    def _weekdays(self) -> Self:
        for day in self.days or []:
            if not 0 <= day <= 6:
                raise ValueError(f"weekday {day} is not 0..6")
        if self.any_of is not None and not self.any_of:
            raise ValueError("any_of needs at least one alternative")
        if self.cardio_minutes is not None and self.cardio_minutes[0] > self.cardio_minutes[1]:
            raise ValueError("cardio_minutes: the low bound is above the high bound")
        return self

    def keys(self) -> set[str]:
        """The checks this block names (`any_of` excluded)."""
        return {
            key
            for key in self.model_fields_set
            if key != "any_of" and getattr(self, key) is not None
        }


class Turn(BaseModel):
    model_config = _FORBID

    user: str = Field(min_length=1)
    expect: Expect = Field(default_factory=Expect)


class LegacyStep(BaseModel):
    """A legacy-mode request or revision. `expect_as` reuses a planner turn's `expect` (its
    0-based index in `turns`) instead of repeating it."""

    model_config = _FORBID

    user: str = Field(min_length=1)
    expect: Expect | None = None
    expect_as: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_expect(self) -> Self:
        if self.expect is not None and self.expect_as is not None:
            raise ValueError("give expect or expect_as, not both")
        return self


class LegacySave(BaseModel):
    model_config = _FORBID

    expect: Expect | None = None
    expect_as: int | None = Field(default=None, ge=0)


class LegacySpec(BaseModel):
    model_config = _FORBID

    request: LegacyStep
    revisions: list[LegacyStep] = Field(default_factory=list)
    save: LegacySave | None = None


class ConversationFixture(BaseModel):
    model_config = _FORBID

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str = ""
    # The name of a profile fixture in `tests/fixtures/llm_eval/`, or the same keys inline.
    profile: str | dict[str, Any]
    profile_overrides: dict[str, Any] = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)
    plans: list[Plan] = Field(default_factory=list)  # saved before the first turn
    turns: list[Turn] = Field(min_length=1)
    legacy: LegacySpec

    @model_validator(mode="after")
    def _expect_as_in_range(self) -> Self:
        steps: list[LegacyStep | LegacySave] = [self.legacy.request, *self.legacy.revisions]
        if self.legacy.save is not None:
            steps.append(self.legacy.save)
        for step in steps:
            if step.expect_as is not None and step.expect_as >= len(self.turns):
                raise ValueError(f"expect_as {step.expect_as} names no turn")
        return self

    def legacy_expect(self, step: LegacyStep | LegacySave) -> Expect:
        if step.expect_as is not None:
            return self.turns[step.expect_as].expect
        return step.expect or Expect()


def load_conversation_fixtures(directory: Path) -> list[ConversationFixture]:
    """Every `*.json` in `directory`, validated, sorted by file name."""
    return [
        ConversationFixture.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def resolve_profile(fixture: ConversationFixture, profiles_dir: Path) -> ProfileSpec:
    """The fixture's profile: a referenced single-turn fixture's profile keys (its history,
    request and ids dropped) or the inline keys, then `profile_overrides`."""
    if isinstance(fixture.profile, str):
        path = profiles_dir / f"{fixture.profile}.json"
        if not path.is_file():
            raise ValueError(f"{fixture.name}: no profile fixture {path.name}")
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("exclusions"):
            raise ValueError(f"{fixture.name}: profile exclusions are not supported here")
        raw = {key: value for key, value in raw.items() if key not in _NON_PROFILE_KEYS}
    else:
        raw = dict(fixture.profile)
    raw.update(fixture.profile_overrides)
    return ProfileSpec.model_validate(raw)


# --- Results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    outcome: CheckOutcome
    detail: str = ""
    soft: bool = False


@dataclass(slots=True)
class TurnRecord:
    index: int
    action: str  # "message" (planner) | "propose" | "revise" | "save" (legacy)
    user: str
    reply: str = ""
    events: list[str] = field(default_factory=list)
    plan: Plan | None = None
    staged: bool = False
    checks: list[CheckResult] = field(default_factory=list)
    latency_s: float = 0.0
    model_calls: int = 0  # agent runs that returned (llm_calls rows with ok = 1)
    failed_calls: int = 0  # agent runs that failed: no tokens are recorded for them
    purposes: tuple[str, ...] = ()  # every agent run's purpose, in order
    guards: tuple[GuardVerdict, ...] = ()  # legacy: the round's fired verdicts
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    models: tuple[str, ...] = ()
    error: str | None = None


@dataclass(slots=True)
class ConversationResult:
    scenario: str
    mode: Mode
    turns: list[TurnRecord] = field(default_factory=list)
    error: str | None = None  # the run could not be set up

    def _hard(self) -> list[CheckResult]:
        return [check for turn in self.turns for check in turn.checks if not check.soft]

    @property
    def checks_passed(self) -> int:
        return sum(1 for check in self._hard() if check.outcome == "pass")

    @property
    def checks_total(self) -> int:
        return sum(1 for check in self._hard() if check.outcome != "n/a")

    @property
    def model_calls(self) -> int:
        return sum(turn.model_calls for turn in self.turns)

    @property
    def failed_calls(self) -> int:
        return sum(turn.failed_calls for turn in self.turns)

    @property
    def input_tokens(self) -> int:
        return sum(turn.input_tokens for turn in self.turns)

    @property
    def output_tokens(self) -> int:
        return sum(turn.output_tokens for turn in self.turns)

    @property
    def cost_usd(self) -> float:
        return sum(turn.cost_usd for turn in self.turns)

    @property
    def cost_known(self) -> bool:
        return all(turn.cost_known for turn in self.turns)

    @property
    def latency_s(self) -> float:
        return sum(turn.latency_s for turn in self.turns)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted({model for turn in self.turns for model in turn.models}))


# --- Checks -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observed:
    """What one turn left behind, as the checks see it."""

    mode: Mode
    reply: str
    staged: bool  # this turn staged a draft
    plan: Plan | None  # the current draft, else the plan saved in this turn
    previous: Plan | None  # the previous turn's plan
    has_draft: bool  # an unsaved draft is current
    saved_match: bool  # a plan version saved in this turn equals the last staged draft
    profile_location: Location
    history_kg: Mapping[str, float] = field(default_factory=dict)
    # Planner mode: `plan` as the model staged it, before the guards' load substitution.
    requested: Plan | None = None
    # Legacy mode: the verdicts the round fired in this turn.
    guards: Sequence[GuardVerdict] = ()


def _items(plan: Plan) -> list[Prescription]:
    return [item for workout in plan.workouts for block in workout.blocks for item in block.items]


def _workout_items(plan: Plan, key: str) -> list[Prescription]:
    for workout in plan.workouts:
        if workout.key == key:
            return [item for block in workout.blocks for item in block.items]
    return []


def _ok(name: str, ok: bool, detail: str = "") -> CheckResult:
    return CheckResult(name, "pass" if ok else "fail", detail)


def _ids(items: Sequence[Prescription]) -> str:
    return ", ".join(sorted({item.exercise_id for item in items}))


def _timed_seconds(item: Prescription, catalog: Catalog) -> int:
    exercise = catalog.by_id(item.exercise_id)
    is_cardio = is_activity_id(item.exercise_id) or (
        exercise is not None and exercise.kind == ExerciseKind.CARDIO
    )
    if not is_cardio or item.measure != Measure.TIME or item.seconds is None:
        return 0
    return item.seconds * item.sets


def cardio_minutes(plan: Plan, catalog: Catalog) -> int:
    """The longest workout's timed cardio and activity work, in whole minutes."""
    per_workout = [
        sum(_timed_seconds(item, catalog) for block in w.blocks for item in block.items)
        for w in plan.workouts
    ]
    return round(max(per_workout, default=0) / 60)


def _changed_items(previous: Plan, current: Plan) -> tuple[int, set[str], bool]:
    """(how many items differ, the ids on either side of a difference, schedule changed).
    Compares each item's full JSON, so a changed note, load, set count or target counts as a
    difference; a block's kind or role and a workout's title do not (only items are
    compared). Items are aligned per workout key with `difflib`, so an inserted or removed
    item counts once instead of shifting everything after it."""
    changed = 0
    ids: set[str] = set()
    keys = {w.key for w in previous.workouts} | {w.key for w in current.workouts}
    for key in sorted(keys):
        before = _workout_items(previous, key)
        after = _workout_items(current, key)
        a = [item.model_dump_json() for item in before]
        b = [item.model_dump_json() for item in after]
        matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            changed += max(i2 - i1, j2 - j1)
            ids.update(item.exercise_id for item in before[i1:i2])
            ids.update(item.exercise_id for item in after[j1:j2])
    schedule_changed = sorted((d.weekday, d.workout_key) for d in previous.schedule) != sorted(
        (d.weekday, d.workout_key) for d in current.schedule
    )
    return changed, ids, schedule_changed


def _changed_ids(previous: Plan, current: Plan) -> tuple[set[str], set[str]]:
    """(exercise ids removed, exercise ids added) across the whole plan, ignoring sets, loads,
    notes and positions: "replace X with Y" is one id out and one in however many workouts
    carry X."""
    before = {item.exercise_id for item in _items(previous)}
    after = {item.exercise_id for item in _items(current)}
    return before - after, after - before


def _max_kg(plan: Plan | None, exercise_id: str) -> float | None:
    if plan is None:
        return None
    kgs = [
        item.load.kg
        for item in _items(plan)
        if item.exercise_id == exercise_id and item.load.kind == "kg" and item.load.kg is not None
    ]
    return max(kgs) if kgs else None


def _plan_checks(expect: Expect, plan: Plan, seen: Observed, catalog: Catalog) -> list[CheckResult]:
    results: list[CheckResult] = []
    items = _items(plan)
    catalog_items = [(item, catalog.by_id(item.exercise_id)) for item in items]
    # Load checks read the plan as the model asked for it (see `_LOAD_RULES`).
    asked = seen.requested if seen.requested is not None else plan
    asked_items = [(item, catalog.by_id(item.exercise_id)) for item in _items(asked)]

    if expect.days is not None:
        got = sorted({day.weekday for day in plan.schedule})
        results.append(_ok("days", got == sorted(set(expect.days)), f"weekdays {got}"))
    if expect.days_count is not None:
        count = len(plan.schedule)
        results.append(_ok("days_count", count == expect.days_count, f"{count} day(s)"))
    if expect.workouts is not None:
        count = len(plan.workouts)
        results.append(_ok("workouts", count == expect.workouts, f"{count} workout(s)"))
    if expect.items_max is not None:
        most = max((len(_workout_items(plan, w.key)) for w in plan.workouts), default=0)
        results.append(_ok("items_max", most <= expect.items_max, f"{most} item(s) at most"))
    if expect.no_kg_loads is not None:
        kg = [item for item, _ in asked_items if item.load.kind == "kg"]
        detail = f"kg on {_ids(kg)}" if kg else ""
        ids = {item.exercise_id for item in items}
        ok = (not kg) == expect.no_kg_loads
        results.append(_load_check("no_kg_loads", ok, detail, seen, ids))
    if expect.no_equipment is not None:
        gear = [item for item, ex in catalog_items if ex is not None and ex.equipment]
        detail = f"equipment on {_ids(gear)}" if gear else ""
        results.append(_ok("no_equipment", (not gear) == expect.no_equipment, detail))
    if expect.exclude_equipment is not None:
        banned = set(expect.exclude_equipment)
        hits = [item for item, ex in catalog_items if ex is not None and banned & set(ex.equipment)]
        results.append(_ok("exclude_equipment", not hits, _ids(hits)))
    if expect.exclude_ids is not None:
        hits = [item for item in items if item.exercise_id in set(expect.exclude_ids)]
        results.append(_ok("exclude_ids", not hits, _ids(hits)))
    if expect.exclude_kinds is not None:
        kinds = set(expect.exclude_kinds)
        hits = [item for item, ex in catalog_items if ex is not None and ex.kind in kinds]
        results.append(_ok("exclude_kinds", not hits, _ids(hits)))
    if expect.include_ids_any is not None:
        present = {item.exercise_id for item in items} & set(expect.include_ids_any)
        results.append(_ok("include_ids_any", bool(present), ", ".join(sorted(present))))
    if _legacy_cannot(expect, "activity_items_min", seen) is not None:
        results.append(CheckResult("activity_items_min", "n/a", LEGACY_NO_ACTIVITY))
    elif expect.activity_items_min is not None:
        count = sum(
            1
            for day in plan.schedule
            for item in _workout_items(plan, day.workout_key)
            if is_activity_id(item.exercise_id)
        )
        ok = count >= expect.activity_items_min
        results.append(_ok("activity_items_min", ok, f"{count} scheduled activity item(s)"))
    if expect.reps_items_max is not None:
        count = sum(1 for item in items if item.measure == Measure.REPS)
        ok = count <= expect.reps_items_max
        results.append(_ok("reps_items_max", ok, f"{count} item(s) in reps"))
    if expect.location is not None:
        where = plan.location or seen.profile_location
        if _legacy_cannot(expect, "location", seen) is not None:
            results.append(CheckResult("location", "n/a", LEGACY_NO_LOCATION))
        else:
            results.append(_ok("location", where == expect.location, where.value))
    if expect.outdoor_allowed is not None:
        indoor = [
            item
            for item, ex in catalog_items
            if ex is not None and Location.OUTDOOR not in ex.locations
        ]
        ok = (not indoor) == expect.outdoor_allowed
        results.append(
            _ok("outdoor_allowed", ok, f"not outdoors: {_ids(indoor)}" if indoor else "")
        )
    if expect.catalog_only is not None:
        outside = [item for item, ex in catalog_items if ex is None]
        results.append(_ok("catalog_only", (not outside) == expect.catalog_only, _ids(outside)))
    if expect.compound_loads is not None:
        allowed = set(expect.compound_loads)
        compound = [
            item for item, ex in asked_items if ex is not None and ex.kind == ExerciseKind.COMPOUND
        ]
        wrong = [item for item in compound if item.load.kind not in allowed]
        ids = {item.exercise_id for item in compound}
        results.append(_load_check("compound_loads", not wrong, _ids(wrong), seen, ids))
    if expect.history_loads is not None:
        seeded = [item for item, _ in asked_items if item.exercise_id in seen.history_kg]
        not_kg = [item for item in seeded if item.load.kind != "kg"]
        if not seeded:
            detail = "no exercise with seeded history in the plan"
        else:
            detail = f"not in kg: {_ids(not_kg)}" if not_kg else _ids(seeded)
        ok = (not not_kg) == expect.history_loads
        ids = {item.exercise_id for item in seeded}
        results.append(_load_check("history_loads", ok, detail, seen, ids))
    if expect.single_change is not None or expect.unchanged_except is not None:
        results.extend(_change_checks(expect, plan, seen.previous))
    if expect.swap_load is not None:
        results.append(_swap_check(expect.swap_load, plan, seen))
    if expect.cardio_minutes is not None:
        low, high = expect.cardio_minutes
        minutes = cardio_minutes(plan, catalog)
        results.append(
            CheckResult(
                "cardio_minutes",
                "pass" if low <= minutes <= high else "fail",
                f"{minutes} min",
                soft=True,
            )
        )
    return results


def _change_checks(expect: Expect, plan: Plan, previous: Plan | None) -> list[CheckResult]:
    names = [
        name
        for name, value in (
            ("single_change", expect.single_change),
            ("unchanged_except", expect.unchanged_except),
        )
        if value is not None
    ]
    if previous is None:
        return [CheckResult(name, "n/a", "no previous plan") for name in names]
    changed, ids, schedule_changed = _changed_items(previous, plan)
    detail = f"{changed} item(s) differ ({', '.join(sorted(ids))})"
    if schedule_changed:
        detail += "; schedule changed"
    results = []
    if expect.single_change is not None:
        removed, added = _changed_ids(previous, plan)
        one = bool(removed | added) and len(removed) <= 1 and len(added) <= 1
        ok = (one and not schedule_changed) == expect.single_change
        swap = f"out: {', '.join(sorted(removed)) or '-'}; in: {', '.join(sorted(added)) or '-'}"
        if schedule_changed:
            swap += "; schedule changed"
        results.append(_ok("single_change", ok, swap))
    if expect.unchanged_except is not None:
        ok = ids <= set(expect.unchanged_except) and not schedule_changed
        results.append(_ok("unchanged_except", ok, detail))
    return results


def _load_guard_hits(seen: Observed, exercise_ids: set[str]) -> list[str]:
    hits = set()
    for verdict in seen.guards:
        if verdict.rule not in _LOAD_RULES and not verdict.rule.startswith(_LOAD_RULE_PREFIXES):
            continue
        for exercise_id in exercise_ids:
            if re.search(rf"\b{re.escape(exercise_id)}\b", verdict.detail):
                hits.add(f"{verdict.rule} on {exercise_id}")
    return sorted(hits)


def _load_check(
    name: str, ok: bool, detail: str, seen: Observed, exercise_ids: set[str]
) -> CheckResult:
    """A load check's result. In legacy mode a load guard that fired on one of the checked
    exercises in this turn fails it: the judged plan shows the guards' load, not the
    model's."""
    if seen.mode == "legacy":
        hits = _load_guard_hits(seen, exercise_ids)
        if hits:
            return CheckResult(name, "fail", f"a load guard fired this turn: {'; '.join(hits)}")
    if seen.requested is not None and detail:
        detail += " (as staged, before the guards)"
    return _ok(name, ok, detail)


def _swap_check(swap: SwapLoad, plan: Plan, seen: Observed) -> CheckResult:
    asked = seen.requested if seen.requested is not None else plan
    swapped = [item for item in _items(asked) if item.exercise_id == swap.to]
    if not swapped:
        return CheckResult("swap_load", "fail", f"{swap.to} is not in the plan")
    reference = _max_kg(seen.previous, swap.from_id) or seen.history_kg.get(swap.from_id)
    worst: Load = max(
        (item.load for item in swapped),
        key=lambda load: load.kg if load.kind == "kg" and load.kg is not None else -1.0,
    )
    if worst.kind != "kg":
        return _load_check("swap_load", True, f"{swap.to}: {worst.kind}", seen, {swap.to})
    assert worst.kg is not None
    if reference is None:
        return CheckResult("swap_load", "fail", f"{swap.to}: {worst.kg:g} kg, no reference")
    # Strictly below: carrying the barbell's number over to dumbbells is the mistake.
    ok = worst.kg < reference
    detail = f"{swap.to}: {worst.kg:g} kg vs {swap.from_id} {reference:g} kg"
    return _load_check("swap_load", ok, detail, seen, {swap.to})


def evaluate(expect: Expect, seen: Observed, catalog: Catalog) -> list[CheckResult]:
    """Every check `expect` names, in a fixed order. A plan check with no plan fails; a check
    that cannot apply (`question` in legacy mode, a change check with no previous plan) is
    n/a."""
    results: list[CheckResult] = []
    if expect.question is not None:
        if seen.mode == "legacy":
            results.append(
                CheckResult("question", "n/a", "the legacy pipeline cannot ask", soft=True)
            )
        else:
            lines = [line for line in seen.reply.splitlines() if line.strip()]
            last = lines[-1] if lines else ""
            asked = not seen.staged and ("?" in last or "？" in last)
            outcome: CheckOutcome = "pass" if asked == expect.question else "fail"
            results.append(CheckResult("question", outcome, "asked" if asked else "", soft=True))
    if expect.draft is not None:
        results.append(_ok("draft", seen.has_draft == expect.draft, f"draft={seen.has_draft}"))
    if expect.saved is not None:
        results.append(_ok("saved", seen.saved_match == expect.saved, f"saved={seen.saved_match}"))
    plan_keys = expect.keys() - {"question", "draft", "saved"}
    if plan_keys:
        if seen.plan is None:
            results.extend(_no_plan(expect, key, seen) for key in sorted(plan_keys))
        else:
            results.extend(_plan_checks(expect, seen.plan, seen, catalog))
    if expect.any_of is not None:
        results.extend(_any_of(expect.any_of, seen, catalog))
    return results


def _legacy_cannot(expect: Expect, key: str, seen: Observed) -> str | None:
    """Why legacy mode cannot pass `key` by construction, or `None`."""
    if seen.mode != "legacy":
        return None
    if key == "location" and expect.location != seen.profile_location:
        return LEGACY_NO_LOCATION
    if key == "activity_items_min" and (expect.activity_items_min or 0) > 0:
        return LEGACY_NO_ACTIVITY
    return None


def _no_plan(expect: Expect, key: str, seen: Observed) -> CheckResult:
    reason = _legacy_cannot(expect, key, seen)
    if reason is not None:
        return CheckResult(key, "n/a", reason)
    return CheckResult(key, "fail", "no plan", soft=key in _SOFT_CHECKS)


def _summary(alternative: Expect) -> str:
    return "+".join(sorted(alternative.keys()))


def _any_of(alternatives: Sequence[Expect], seen: Observed, catalog: Catalog) -> list[CheckResult]:
    """Passes when one alternative passes: its hard checks that apply (n/a ones aside) all
    pass, or, for an alternative of soft checks only, those pass. When any alternative is
    soft only (a question), `any_of` itself is soft: it can be met by asking."""
    soft_only = any(not (alternative.keys() - _SOFT_CHECKS) for alternative in alternatives)
    misses: list[str] = []
    for number, alternative in enumerate(alternatives, start=1):
        checks = evaluate(alternative, seen, catalog)
        hard = [check for check in checks if not check.soft and check.outcome != "n/a"]
        soft = [check for check in checks if check.soft]
        judged = hard if hard else [check for check in soft if check.outcome != "n/a"]
        if judged and all(check.outcome == "pass" for check in judged):
            detail = f"alternative {number} ({_summary(alternative)})"
            extra = [check for check in soft if check not in judged]
            return [CheckResult("any_of", "pass", detail, soft=soft_only), *extra]
        failed = [
            f"{c.name}: {c.detail}" if c.detail else c.name for c in checks if c.outcome != "pass"
        ]
        misses.append(f"{number} ({'; '.join(failed)})")
    detail = "no alternative passed: " + " | ".join(misses)
    return [CheckResult("any_of", "fail", detail, soft=soft_only)]


# --- Seeding ----------------------------------------------------------------------------------


async def _sync_search_index(db: Database, catalog: Catalog) -> None:
    """What `fitme serve` does at startup (ADR 0007): the derived search tables and vectors
    from the shipped index file; a missing or unreadable file only turns semantic ranking
    off."""
    try:
        await catalog_index.sync_database(db, catalog, catalog_index.default_index_path())
    except SearchIndexFileError:
        await catalog_index.sync_database(db, catalog, None)
    await exercise_search.load_runtime(db, catalog)


def _history_document(entries: Sequence[HistoryEntry]) -> str:
    today = clock.now().date()
    by_day: dict[int, list[HistoryEntry]] = {}
    for entry in entries:
        by_day.setdefault(entry.days_ago, []).append(entry)
    sessions = []
    for days_ago in sorted(by_day, reverse=True):
        sets = [
            {
                "exercise": entry.exercise_id,
                "reps": entry.reps,
                "set_index": index,
                **({"kg": entry.kg} if entry.kg is not None else {}),
            }
            for entry in by_day[days_ago]
            for index in range(1, entry.sets + 1)
        ]
        day = today - timedelta(days=days_ago)
        sessions.append({"date": day.isoformat(), "set": sets})
    return json.dumps({"meta": {"version": 1, "timezone": "UTC"}, "session": sessions})


async def seed(
    db: Database, settings: Settings, fixture: ConversationFixture, profile: ProfileSpec
) -> int:
    """The one user of a fresh database, as finishing setup leaves it, plus the fixture's
    history (imported) and saved plans (judged and confirmed like any draft). Returns the
    user id."""
    flags = {flag.value: "no" for flag in ScreeningFlag}
    flags.update({entry["flag"]: entry["value"] for entry in profile.flags})
    async with db.transaction() as conn:
        user_id = await insert_user(conn, language=profile.language, timezone="UTC")
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket=profile.age_bucket,
            weight_bucket=profile.weight_bucket,
            experience=profile.experience,
            barbell_experience=profile.barbell_experience,
            preferences=list(profile.preferences),
            location=profile.location.value,
            equipment=[item.value for item in profile.equipment],
            sessions_per_week=profile.sessions_per_week,
            session_minutes=profile.session_minutes,
            focus=profile.focus,
            completed_at=clock.now(),
            sport=profile.sport,
            sport_sessions_per_week=profile.sport_sessions_per_week,
        )
        for flag, value in sorted(flags.items()):
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag, value=value, clearance=None
            )
        await delete_setup_progress(conn, user_id)
    await _sync_search_index(db, load_catalog())

    if fixture.history:
        report = await history_import.import_history(
            db, settings, _history_document(fixture.history), fmt="json", source="eval"
        )
        if report.rejected or report.unknown_exercises:
            reasons = [r.reason for r in report.rejected] + list(report.unknown_exercises)
            raise ValueError(f"{fixture.name}: history not imported: {'; '.join(reasons)}")
    for plan in fixture.plans:
        drafted = await planning.record_edit_draft(
            db, settings, user_id, plan=plan, plan_id=None, user_report={"source": "eval_seed"}
        )
        if drafted.round is None:
            raise ValueError(f"{fixture.name}: seeded plan refused: {'; '.join(drafted.errors)}")
        confirmed = await planning.confirm_plan(db, settings, user_id, drafted.round.decision_id)
        if confirmed.status != planning.ConfirmStatus.SAVED:
            raise ValueError(f"{fixture.name}: seeded plan not saved ({confirmed.status})")
    return user_id


# --- Running ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Run:
    """One conversation's state across turns."""

    db: Database
    llm: LlmRuntime
    user_id: int
    lang: str
    mode: Mode
    profile_location: Location
    history_kg: dict[str, float]
    catalog: Catalog
    last_staged: Plan | None = None
    last_requested: Plan | None = None  # planner: `last_staged` before load substitution
    previous: Plan | None = None
    last_call_id: int = 0


async def _new_calls(run: _Run, turn: TurnRecord) -> None:
    async with run.db.read() as conn:
        calls = [
            c for c in await list_llm_calls_since(conn, "1970-01-01") if c.id > run.last_call_id
        ]
    if calls:
        run.last_call_id = max(call.id for call in calls)
    done = [call for call in calls if call.ok]
    turn.model_calls = len(done)
    turn.failed_calls = len(calls) - len(done)
    turn.purposes = tuple(call.purpose for call in calls)
    turn.input_tokens = sum(call.input_tokens for call in done)
    turn.output_tokens = sum(call.output_tokens for call in done)
    turn.cost_usd = sum(call.cost_estimate_usd or 0.0 for call in done)
    # Unknown only when a model that answered has no price entry (`FITME_PRICES_FILE`).
    turn.cost_known = all(call.cost_estimate_usd is not None for call in done)
    turn.models = tuple(sorted({call.model for call in calls}))
    turn.events.extend(f"model call failed: {call.purpose}" for call in calls if not call.ok)


async def _current_draft(run: _Run) -> Plan | None:
    draft_id = await planning.current_draft_id(run.db, run.user_id)
    if draft_id is None:
        return None
    return await planning.draft_base_plan(
        run.db, run.user_id, plan_id=None, draft_decision_id=draft_id
    )


async def _saved_versions(run: _Run) -> dict[int, Plan]:
    """Every saved plan version of the user, by version id."""
    async with run.db.read() as conn:
        plans = await list_plans_for_user(conn, run.user_id)
        versions = [v for plan in plans for v in await list_plan_versions(conn, plan.id)]
    return {version.id: Plan.model_validate(version.body) for version in versions}


async def _finish_turn(
    run: _Run,
    turn: TurnRecord,
    expect: Expect,
    saved_now: bool,
    versions_before: set[int],
) -> None:
    await _new_calls(run, turn)
    draft = await _current_draft(run)
    # Only versions written during this turn count: a seeded plan never makes `saved` pass.
    new = [body for vid, body in (await _saved_versions(run)).items() if vid not in versions_before]
    saved_match = run.last_staged is not None and run.last_staged in new
    plan = draft if draft is not None else (run.last_staged if saved_now else None)
    turn.plan = plan
    requested = None
    if run.mode == "planner" and plan is not None and plan == run.last_staged:
        requested = run.last_requested
    seen = Observed(
        mode=run.mode,
        reply=turn.reply,
        staged=turn.staged,
        plan=plan,
        previous=run.previous,
        has_draft=draft is not None,
        saved_match=saved_match,
        profile_location=run.profile_location,
        history_kg=run.history_kg,
        requested=requested,
        guards=turn.guards,
    )
    turn.checks = evaluate(expect, seen, run.catalog)
    if plan is not None:
        run.previous = plan


@dataclass(frozen=True, slots=True)
class _ItemSeen:
    line: str
    staged: Plan | None = None  # the judged draft
    requested: Plan | None = None  # the same draft as staged, before load substitution
    saved: bool = False


def _describe_item(item: assistant.Item) -> _ItemSeen:
    """What one planner turn item did, for the transcript and the checks."""
    if isinstance(item, assistant.DraftUpdated):
        result = item.result
        if result.round is None:
            return _ItemSeen(f"draft not staged: {'; '.join(result.errors)}")
        fired = [v.rule for v in result.round.guards_fired]
        line = f"draft staged (decision {result.round.decision_id}, whole={item.whole})"
        if fired:
            line += f"; guards fired: {', '.join(fired)}"
        return _ItemSeen(line, staged=result.round.plan, requested=result.requested)
    if isinstance(item, assistant.DraftSaved):
        status = item.confirm.status
        saved = status == planning.ConfirmStatus.SAVED
        return _ItemSeen(f"save: {status.value} (version {item.confirm.version})", saved=saved)
    if isinstance(item, assistant.Notice):
        details = f" ({'; '.join(item.details)})" if item.details else ""
        return _ItemSeen(f"notice: {item.key}{details}")
    return _ItemSeen(type(item).__name__)


async def _scan(run: _Run, text: str, turn: TurnRecord) -> int | None:
    """The bot's order for a message: save it, link it to the active session, then the
    stop-word scan. Returns the saved message's id, or `None` when the scan halted (nothing
    reaches a model)."""
    active = await training.active_session(run.db, run.user_id)
    message_id = await record_incoming_text(
        run.db,
        user_id=run.user_id,
        session_id=None if active is None else active.session.id,
        text=text,
    )
    await conversations.link_to_active(run.db, run.user_id, message_id)
    halted = await scan_and_maybe_halt(run.db, user_id=run.user_id, lang=run.lang, text=text)
    if halted is not None:
        turn.reply = "(stop-word scan halted the conversation)"
        turn.events.append("halted")
        return None
    return message_id


async def _planner_turn(run: _Run, index: int, spec: Turn) -> TurnRecord:
    turn = TurnRecord(index=index, action="message", user=spec.user)
    versions_before = set(await _saved_versions(run))
    started = time.monotonic()
    saved_now = False
    try:
        message_id = await _scan(run, spec.user, turn)
        if message_id is not None:
            result = await assistant.handle_message(
                run.db, run.llm, run.user_id, spec.user, chat_message_id=message_id
            )
            if result.refusal is not None:
                turn.reply = result.refusal.message
                turn.events.append(f"refusal: {result.refusal.code.value}")
            else:
                turn.reply = result.message or ""
            for item in result.items:
                seen = _describe_item(item)
                turn.events.append(seen.line)
                if seen.staged is not None:
                    turn.staged = True
                    run.last_staged = seen.staged
                    run.last_requested = seen.requested
                saved_now = saved_now or seen.saved
    except Exception as exc:  # recorded, not raised: one bad turn must not end the eval
        turn.error = f"{type(exc).__name__}: {exc}"
    turn.latency_s = time.monotonic() - started
    await _finish_turn(run, turn, spec.expect, saved_now, versions_before)
    return turn


def _round_reply(result: planning.PlanRoundResult) -> str:
    if result.refusal is not None:
        return result.refusal.message
    return f"draft {result.decision_id}"


async def _legacy_turn(run: _Run, index: int, action: str, text: str, expect: Expect) -> TurnRecord:
    turn = TurnRecord(index=index, action=action, user=text)
    versions_before = set(await _saved_versions(run))
    started = time.monotonic()
    saved_now = False
    try:
        if action == "save":
            draft_id = await planning.current_draft_id(run.db, run.user_id)
            if draft_id is None:
                turn.reply = "(no draft to save)"
            else:
                confirm = await planning.confirm_plan(
                    run.db, run.llm.settings, run.user_id, draft_id
                )
                saved_now = confirm.status == planning.ConfirmStatus.SAVED
                turn.reply = f"save: {confirm.status.value} (version {confirm.version})"
        elif await _scan(run, text, turn) is not None:
            if action == "propose":
                result = await planning.propose_new_plan(
                    run.db, run.llm, run.user_id, guidance=text
                )
            else:
                draft_id = await planning.current_draft_id(run.db, run.user_id)
                if draft_id is None:
                    raise planning.PlanNotFoundError("no current draft to revise")
                result = await planning.revise_plan(
                    run.db, run.llm, run.user_id, planning.DraftBase(draft_id), text
                )
            turn.reply = _round_reply(result)
            if result.refusal is not None:
                turn.events.append(f"refusal: {result.refusal.code.value}")
            turn.guards = tuple(result.guards_fired)
            fired = [v.rule for v in result.guards_fired]
            if fired:
                turn.events.append(f"guards fired: {', '.join(fired)}")
            if result.plan is not None:
                turn.staged = True
                run.last_staged = result.plan
    except Exception as exc:  # recorded, not raised
        turn.error = f"{type(exc).__name__}: {exc}"
    turn.latency_s = time.monotonic() - started
    await _finish_turn(run, turn, expect, saved_now, versions_before)
    return turn


async def run_conversation(
    fixture: ConversationFixture,
    *,
    mode: Mode,
    llm: LlmRuntime,
    profiles_dir: Path,
    work_dir: Path,
) -> ConversationResult:
    """One fixture in one mode, on a fresh database under `work_dir`."""
    result = ConversationResult(scenario=fixture.name, mode=mode)
    catalog = load_catalog()
    db = await open_database(work_dir / f"{fixture.name}-{mode}.db")
    try:
        await migrate(db)
        profile = resolve_profile(fixture, profiles_dir)
        user_id = await seed(db, llm.settings, fixture, profile)
        history_kg: dict[str, float] = {}
        for entry in fixture.history:
            if entry.kg is not None:
                history_kg[entry.exercise_id] = max(entry.kg, history_kg.get(entry.exercise_id, 0))
        run = _Run(
            db=db,
            llm=llm,
            user_id=user_id,
            lang=profile.language,
            mode=mode,
            profile_location=profile.location,
            history_kg=history_kg,
            catalog=catalog,
        )
        if mode == "planner":
            for index, spec in enumerate(fixture.turns):
                result.turns.append(await _planner_turn(run, index, spec))
        else:
            legacy = fixture.legacy
            steps: list[tuple[str, str, Expect]] = [
                ("propose", legacy.request.user, fixture.legacy_expect(legacy.request))
            ]
            steps.extend(
                ("revise", step.user, fixture.legacy_expect(step)) for step in legacy.revisions
            )
            if legacy.save is not None:
                steps.append(("save", "(save)", fixture.legacy_expect(legacy.save)))
            for index, (action, text, expect) in enumerate(steps):
                result.turns.append(await _legacy_turn(run, index, action, text, expect))
    except Exception as exc:  # a setup failure is reported for this run, not raised
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        exercise_search.forget_runtime(db)
        await db.close()
    return result


async def run_conversations(
    *,
    fixtures_dir: Path,
    modes: Sequence[Mode],
    llm: LlmRuntime,
    scenario: str | None = None,
    profiles_dir: Path | None = None,
) -> list[ConversationResult]:
    """Every fixture in `fixtures_dir` (or only `scenario`) in every requested mode. `llm`
    resolves each agent's model (a real runtime in the CLI, `FunctionModel` factories in a
    test). Profile references resolve in `profiles_dir` (default: the parent directory, i.e.
    `tests/fixtures/llm_eval/`)."""
    fixtures = load_conversation_fixtures(fixtures_dir)
    if scenario is not None:
        fixtures = [fixture for fixture in fixtures if fixture.name == scenario]
        if not fixtures:
            raise ValueError(f"no conversation fixture named {scenario!r}")
    profiles = profiles_dir if profiles_dir is not None else fixtures_dir.parent
    results: list[ConversationResult] = []
    with tempfile.TemporaryDirectory(prefix="fitme-eval-") as work:
        for fixture in fixtures:
            for mode in modes:
                results.append(
                    await run_conversation(
                        fixture, mode=mode, llm=llm, profiles_dir=profiles, work_dir=Path(work)
                    )
                )
    return results


def with_model_override(settings: Settings, model: str) -> Settings:
    """`settings` with every agent and every tier resolved to `model` (as
    `FITME_LLM_AGENT_<NAME>` and `FITME_LLM_TIER_*` would), so the large-tier escalation runs
    it too and the recorded model name and its price entry match what actually ran."""
    from fitme.llm.models import AGENT_DEFAULT_TIER

    overridden = settings.model_copy(
        update={"llm_tier_large": model, "llm_tier_medium": model, "llm_tier_small": model}
    )
    overridden._llm_agent_overrides = dict.fromkeys(AGENT_DEFAULT_TIER, model)
    return overridden


# --- Report -----------------------------------------------------------------------------------


def _cost(result: ConversationResult) -> str:
    return f"${result.cost_usd:.4f}" if result.cost_known else "unknown"


def _failed_text(count: int) -> str:
    return f"{count} failed calls, tokens not recorded" if count else "0"


def _metric_rows(result: ConversationResult | None) -> list[str]:
    if result is None:
        return ["—"] * 9
    if result.error is not None:
        return ["setup failed"] + ["—"] * 8
    return [
        f"{result.checks_passed}/{result.checks_total}",
        str(len(result.turns)),
        str(result.model_calls),
        _failed_text(result.failed_calls),
        f"{result.input_tokens} / {result.output_tokens}",
        _cost(result),
        f"{result.latency_s:.1f} s",
        ", ".join(result.models) or "—",
        str(sum(1 for turn in result.turns if turn.error)),
    ]


_METRICS = (
    "checks passed",
    "turns",
    "model calls",
    "failed calls",
    "tokens in / out",
    "estimated cost",
    "latency",
    "models",
    "turn errors",
)


def _by_scenario(
    results: Sequence[ConversationResult],
) -> dict[str, dict[Mode, ConversationResult]]:
    grouped: dict[str, dict[Mode, ConversationResult]] = {}
    for result in results:
        grouped.setdefault(result.scenario, {})[result.mode] = result
    return grouped


def render_summary(results: Sequence[ConversationResult]) -> str:
    """One table, a row per scenario and mode: what the CLI prints."""
    lines = [
        "| scenario | mode | checks | turns | model calls | failed calls | tokens in / out "
        "| cost | latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        row = _metric_rows(result)
        lines.append(f"| {result.scenario} | {result.mode} | {' | '.join(row[:7])} |")
    total_cost = sum(r.cost_usd for r in results)
    known = all(r.cost_known for r in results)
    lines.append("")
    lines.append(
        f"Total estimated cost: ${total_cost:.4f}"
        if known
        else f"Total estimated cost: at least ${total_cost:.4f} (a model has no price entry)"
    )
    return "\n".join(lines)


def _load_text(load: Load) -> str:
    return f"{load.kg:g} kg" if load.kind == "kg" and load.kg is not None else load.kind


def _target_text(item: Prescription) -> str:
    if item.measure == Measure.TIME and item.seconds is not None:
        seconds = item.seconds
        target = f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"
    elif item.measure == Measure.DISTANCE:
        target = f"{item.meters} m"
    elif item.reps_min == item.reps_max:
        target = str(item.reps_min)
    else:
        target = f"{item.reps_min}–{item.reps_max}"
    return f"{item.sets}×{target}"


def plan_lines(plan: Plan, catalog: Catalog) -> list[str]:
    """One line per item: `day workout: name (id), sets×target, load`."""
    days: dict[str, list[str]] = {}
    for day in sorted(plan.schedule, key=lambda d: d.weekday):
        days.setdefault(day.workout_key, []).append(_WEEKDAYS[day.weekday])
    where = plan.location.value if plan.location is not None else "profile location"
    lines = [f"plan: {plan.name} ({where})"]
    for workout in plan.workouts:
        when = "/".join(days.get(workout.key, [])) or "unscheduled"
        for block in workout.blocks:
            for item in block.items:
                exercise = catalog.by_id(item.exercise_id)
                if exercise is not None:
                    name = exercise.names.get("en") or item.exercise_id
                else:
                    name = item.activity_name or item.exercise_id
                lines.append(
                    f"{when} {workout.key}: {name} ({item.exercise_id}), "
                    f"{_target_text(item)}, {_load_text(item.load)}"
                )
    return lines


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines() or [""])


def _transcript(result: ConversationResult, catalog: Catalog) -> list[str]:
    lines = [f"### {result.mode}", ""]
    if result.error is not None:
        lines.extend([f"Setup failed: `{result.error}`", ""])
        return lines
    for turn in result.turns:
        cost = f"${turn.cost_usd:.4f}" if turn.cost_known else "cost unknown"
        failed = f" ({_failed_text(turn.failed_calls)})" if turn.failed_calls else ""
        lines.append(
            f"**Turn {turn.index + 1} ({turn.action})** — {turn.latency_s:.1f} s, "
            f"{turn.model_calls} model call(s){failed}, {turn.input_tokens}/{turn.output_tokens} "
            f"tokens, {cost}"
        )
        lines.append("")
        lines.append("User:")
        lines.append(_quote(turn.user))
        lines.append("")
        lines.append("Reply:")
        lines.append(_quote(turn.reply or "(no text)"))
        lines.append("")
        if turn.error:
            lines.append(f"Error: `{turn.error}`")
            lines.append("")
        if turn.events:
            lines.extend(f"- {event}" for event in turn.events)
            lines.append("")
        if turn.plan is not None:
            lines.append("```")
            lines.extend(plan_lines(turn.plan, catalog))
            minutes = cardio_minutes(turn.plan, catalog)
            if minutes:
                lines.append(f"(longest workout's timed cardio and activity work: {minutes} min)")
            lines.append("```")
            lines.append("")
        if turn.checks:
            for check in turn.checks:
                soft = " (reported, not counted)" if check.soft else ""
                detail = f" — {check.detail}" if check.detail else ""
                lines.append(f"- {check.outcome}: {check.name}{detail}{soft}")
            lines.append("")
    final = next((turn.plan for turn in reversed(result.turns) if turn.plan is not None), None)
    if final is not None:
        lines.extend(
            [
                "<details><summary>Last plan as JSON</summary>",
                "",
                "```json",
                final.model_dump_json(indent=2, exclude_none=True),
                "```",
                "",
                "</details>",
                "",
            ]
        )
    return lines


def render_report(
    results: Sequence[ConversationResult], *, title_time: datetime | None = None
) -> str:
    """The Markdown report: a summary table, per scenario a planner/legacy table and both
    transcripts, then the fixed closing paragraph."""
    catalog = load_catalog()
    stamp = (title_time or datetime.now()).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# LLM-first planning: conversation eval ({stamp})",
        "",
        "Each scenario runs as a conversation through the conversational planner "
        "(`services.assistant.handle_message`) and the same requests through the legacy "
        "pipeline (`propose_new_plan`, then `revise_plan` per follow-up, then "
        "`confirm_plan`), each on a fresh database seeded from the fixture.",
        "",
        LEGEND,
        "",
        "## Summary",
        "",
        render_summary(results),
        "",
    ]
    for scenario, by_mode in _by_scenario(results).items():
        lines.extend([f"## {scenario}", ""])
        planner_rows = _metric_rows(by_mode.get("planner"))
        legacy_rows = _metric_rows(by_mode.get("legacy"))
        lines.append("| | planner | legacy |")
        lines.append("|---|---|---|")
        for metric, planner_value, legacy_value in zip(
            _METRICS, planner_rows, legacy_rows, strict=True
        ):
            lines.append(f"| {metric} | {planner_value} | {legacy_value} |")
        lines.append("")
        for mode in MODES:
            if mode in by_mode:
                lines.extend(_transcript(by_mode[mode], catalog))
    lines.extend(["## Reading this report", "", CLOSING_PARAGRAPH, ""])
    return "\n".join(lines)


def write_report(
    results: Sequence[ConversationResult], out_dir: Path, *, now: datetime | None = None
) -> Path:
    """`out_dir/llm_first_eval_<YYYY-MM-DD_HHMMSS>.md` (local time); returns the path."""
    stamp = now or datetime.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"llm_first_eval_{stamp.strftime('%Y-%m-%d_%H%M%S')}.md"
    path.write_text(render_report(results, title_time=stamp), encoding="utf-8")
    return path
