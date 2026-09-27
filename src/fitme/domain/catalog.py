"""Exercise catalog domain model (A§4.4).

Loading `exercises.toml` from disk is M3 (`fitme.catalog`); this module only defines and
validates its shape, so guards and their tests can build a `Catalog` in memory without any
file I/O.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from fitme.domain.enums import VALID_LOADS_AREAS, Equipment, ExerciseKind, Location, ScreeningFlag
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
    equipment: list[Equipment]
    locations: list[Location]
    # Catalog short names for the areas this exercise loads, e.g. "knee", "lower_back"
    # (A§4.4). Matches `domain.enums.AREA_FLAG_TO_LOADS_AREA`'s values, not its keys.
    loads_areas: list[str] = Field(default_factory=list)
    contraindicated_by: list[ScreeningFlag] = Field(default_factory=list)
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
