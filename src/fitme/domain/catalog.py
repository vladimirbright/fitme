"""Exercise catalog domain model (A§4.4).

Loading `exercises.toml` from disk is M3 (`fitme.catalog`); this module only defines and
validates its shape, so guards and their tests can build a `Catalog` in memory without any
file I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from fitme.domain.enums import (
    LOADS_AREA_TO_AREA_FLAG,
    VALID_LOADS_AREAS,
    Equipment,
    ExerciseKind,
    ExercisePattern,
    Location,
    ScreeningFlag,
)
from fitme.domain.models import Load

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

# M2 addition: exercises round to the implement's step. Barbell exercises load in 2.5 kg
# plates per side typically; everything else (dumbbells, machines, bodyweight) is finer.
_BARBELL_LOAD_STEP_KG = 2.5
_DEFAULT_LOAD_STEP_KG = 1.0


class Exercise(BaseModel):
    """One `[[exercise]]` entry (A§4.4)."""

    model_config = _STRICT_CONFIG

    id: str
    names: dict[str, str]  # lang -> display name
    kind: ExerciseKind
    # M3 addition: the movement pattern this exercise trains (`ExercisePattern`). Used by
    # `services.catalog.available_exercises` and its "every location has a workable
    # full-body set" test to check that each location offers every primary pattern.
    pattern: ExercisePattern
    equipment: list[Equipment]
    locations: list[Location]
    # Catalog short names for the areas this exercise loads, e.g. "knee", "lower_back"
    # (A§4.4). Matches `domain.enums.AREA_FLAG_TO_LOADS_AREA`'s values, not its keys.
    loads_areas: list[str] = Field(default_factory=list)
    contraindicated_by: list[ScreeningFlag] = Field(default_factory=list)
    # A§4.4 catalog invariant: every area in `loads_areas` must have its injury flag in
    # `contraindicated_by`. This is the explicit, reviewed escape hatch for an area that's
    # genuinely fine to leave uncovered — none are expected in the shipped catalog, and each
    # entry here should carry a `#` comment in the TOML file explaining why. Checked by
    # `missing_contraindication_coverage` below and `fitme catalog check`, not at load time:
    # a bad entry should fail review, not crash every test that builds a minimal `Exercise`.
    contraindication_exceptions: list[str] = Field(default_factory=list)
    increment_kg: float = Field(gt=0)
    start: Load  # conservative calibration start (A§4.4): an empty bar, the lightest
    # dumbbell/machine stack, or bodyweight.
    instructions: dict[str, str]  # lang -> vetted instructions text, shown during training
    # Rounding step for the load engine (A§7.3). Defaults from `equipment` when omitted: see
    # `_default_load_step_kg` below.
    load_step_kg: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _loads_areas_are_known(self) -> Exercise:
        unknown = sorted(set(self.loads_areas) - VALID_LOADS_AREAS)
        if unknown:
            raise ValueError(
                f"{self.id}: unknown loads_areas {unknown}; must be one of "
                f"{sorted(VALID_LOADS_AREAS)}"
            )
        return self

    @model_validator(mode="after")
    def _kg_capable_exercises_declare_loads_areas(self) -> Exercise:
        """An exercise whose `start` (or, in a later milestone, any other allowed load) can be
        a `kg` number must declare which body areas it loads: `checkins.increase_allowed`
        (A§7) can only gate a numeric increase against areas it's told about, and a kg-capable
        exercise with no declared area would otherwise let an increase through unchecked."""
        if self.start.kind == "kg" and not self.loads_areas:
            raise ValueError(
                f"{self.id}: a kg-capable exercise (start.kind == 'kg') must have a "
                "non-empty loads_areas (needed for check-in gating, A§7)"
            )
        return self

    @model_validator(mode="after")
    def _contraindication_exceptions_are_loaded_areas(self) -> Exercise:
        unknown = sorted(set(self.contraindication_exceptions) - set(self.loads_areas))
        if unknown:
            raise ValueError(
                f"{self.id}: contraindication_exceptions {unknown} are not in loads_areas"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _default_load_step_kg(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        if data.get("load_step_kg") is not None:
            return data
        equipment = data.get("equipment") or []
        # `equipment` is still raw (str) data at this point, before enum coercion.
        is_barbell = Equipment.BARBELL.value in equipment
        data = dict(data)
        data["load_step_kg"] = _BARBELL_LOAD_STEP_KG if is_barbell else _DEFAULT_LOAD_STEP_KG
        return data


class Catalog(BaseModel):
    """The whole `exercises.toml` file (A§4.4): `[[exercise]]` maps to this model's
    `exercise` field, matching what `tomllib.load()` produces exactly."""

    model_config = _STRICT_CONFIG

    exercise: list[Exercise]

    _by_id: dict[str, Exercise] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _build_index(self) -> Catalog:
        index: dict[str, Exercise] = {}
        for item in self.exercise:
            if item.id in index:
                raise ValueError(f"duplicate catalog exercise id: {item.id}")
            index[item.id] = item
        self._by_id = index
        return self

    def by_id(self, exercise_id: str) -> Exercise | None:
        return self._by_id.get(exercise_id)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)


# M3 addition: canonical equipment available at each `Location`, used by the service layer to
# build `guards.context.GuardContext.equipment` (see that field's docstring) and by
# `services.catalog.available_exercises`.
#
# - A public/studio gym is assumed to have every catalog `Equipment` value, including
#   `machine`/`cable`: A§5.1's equipment multi-select is only asked for "home with
#   equipment", so a gym location must pass the *full* set or every barbell/rack/machine/
#   cable exercise would be wrongly rejected there.
# - "Home with equipment" has no default: it's exactly the user's own multi-select answer
#   (A§5.1 step 9, restricted to `HOME_SELECTABLE_EQUIPMENT` below), so it's deliberately
#   absent from this mapping — callers use the profile's `equipment` field directly instead
#   of looking it up here.
# - An apartment with no equipment has none, by definition.
# - Outdoor is treated as having a pull-up bar and nothing else: the common case is a park
#   with a fixed bar/frame (a "pull-up bar" in the sense of A§4.2's `Equipment` enum), but no
#   free weights or bench. A user without access to one still gets a workable outdoor session
#   because every location keeps at least one exercise per movement pattern using no
#   equipment at all (`fitme.catalog`'s "every location has a workable full-body set" test).
LOCATION_DEFAULT_EQUIPMENT: dict[Location, frozenset[Equipment]] = {
    Location.PUBLIC_GYM: frozenset(Equipment),
    Location.STUDIO_GYM: frozenset(Equipment),
    Location.APARTMENT_NO_EQUIPMENT: frozenset(),
    Location.OUTDOOR: frozenset({Equipment.PULL_UP_BAR}),
}

# A§4.2: `machine` and `cable` are gym-only. A home gym can plausibly hold any of the other
# five (a barbell, a rack, dumbbells, a bench, a pull-up bar, a kettlebell, bands), but a
# leg-press machine or a cable stack is not something the setup questionnaire should offer as
# a home-equipment option (A§5.1 step 9's equipment multi-select uses this set, not the full
# `Equipment` enum).
HOME_SELECTABLE_EQUIPMENT: frozenset[Equipment] = frozenset(Equipment) - frozenset(
    {Equipment.MACHINE, Equipment.CABLE}
)


def missing_contraindication_coverage(catalog: Catalog) -> list[str]:
    """A§4.4 catalog invariant: every exercise's `contraindicated_by` must cover every area it
    loads — `contraindicated_by ⊇ {<area>_injury_current for area in loads_areas}` — unless
    the area is named in that exercise's own `contraindication_exceptions`. Returns one
    human-readable problem per violation, in catalog order; an empty list means the catalog
    satisfies the invariant. Used by `fitme catalog check` and its tests; not run at load
    time (see `Exercise.contraindication_exceptions`'s docstring for why)."""
    problems: list[str] = []
    for exercise in catalog.exercise:
        for area in exercise.loads_areas:
            if area in exercise.contraindication_exceptions:
                continue
            flag = LOADS_AREA_TO_AREA_FLAG.get(area)
            if flag is None:
                continue  # unreachable: `loads_areas` is validated against known areas above
            if flag not in exercise.contraindicated_by:
                problems.append(
                    f"{exercise.id}: loads {area!r} but does not contraindicate {flag.value!r}"
                )
    return problems
