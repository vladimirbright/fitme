"""ADR 0011: how a prescription's intensity is set and said, by the exercise's stimulus
(`domain.catalog.intensity_mode`), shared by the plan view, the workout blocks and the web.

- **Reps in reserve** belong to loaded (or bodyweight) strength, hypertrophy and endurance
  sets in reps. A throw, a jump, a sprint, a footwork drill, a carry or an interval has no
  meaningful "reps left": an `rir` on one is dropped when a plan is judged
  (`repair_intensity`) and never shown on a plan saved before.
- **Unknown loads stay unknown.** With no history the load engine prescribes `calibration`,
  and the text says how to choose the load for *that* stimulus — a weight leaving about N
  reps in reserve, a load that moves fast, a carry held upright for the whole distance, a pace
  that holds for every interval. The catalog `start` (the lightest implement) is never shown
  as a starting weight: "start with 2 kg" for a farmer carry is a number, not a judgement.
"""

from __future__ import annotations

from fitme.domain.catalog import Catalog, Exercise, intensity_mode
from fitme.domain.enums import BlockRole, IntensityMode, Measure
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load, Plan, Prescription
from fitme.i18n import t

RIR_DROPPED_RULE = "intensity.rir_dropped"
# A calibration load on a loaded exercise regulated by effort says how hard the first sets
# should feel ("leaves about 3 reps in reserve"). ADR 0009.
DEFAULT_CALIBRATION_RIR = 3

# Blocks whose speed work is easy or done for capacity: no "stop when speed drops" cue.
_NO_SPEED_CUE_ROLES = frozenset({BlockRole.WARM_UP, BlockRole.CONDITIONING, BlockRole.MOBILITY})

_CALIBRATION_KEYS = {
    IntensityMode.SPEED: "load_calibration_speed",
    IntensityMode.CARRY: "load_calibration_carry",
    IntensityMode.PACE: "load_calibration_pace",
}


def mode_of(prescription: Prescription, exercise: Exercise | None) -> IntensityMode:
    """The prescription's intensity mode; `EFFORT` for an id outside the catalog."""
    if exercise is None:
        return IntensityMode.EFFORT
    return intensity_mode(exercise, prescription.measure)


def rir_applies(prescription: Prescription, exercise: Exercise | None) -> bool:
    """Reps in reserve mean something for this prescription: reps, regulated by effort."""
    return (
        prescription.measure == Measure.REPS
        and mode_of(prescription, exercise) == IntensityMode.EFFORT
    )


def repair_intensity(plan: Plan, catalog: Catalog) -> list[GuardVerdict]:
    """In place, when a plan is judged: drop an `rir` where it doesn't apply (power, speed,
    carries, intervals, holds), then give every calibration load regulated by effort the
    default `rir` (after load substitution, so an engine-chosen calibration gets it too).
    Returns one informational verdict per dropped `rir` for `decisions.guards_fired`."""
    fired: list[GuardVerdict] = []
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                exercise = catalog.by_id(prescription.exercise_id)
                if exercise is None:
                    continue
                if prescription.rir is not None and not rir_applies(prescription, exercise):
                    mode = mode_of(prescription, exercise)
                    fired.append(
                        GuardVerdict(
                            rule=RIR_DROPPED_RULE,
                            ok=True,
                            detail=(
                                f"{exercise.id}: rir {prescription.rir} dropped; intensity "
                                f"is {mode.value}, not reps in reserve"
                            ),
                        )
                    )
                    prescription.rir = None
                if (
                    prescription.rir is None
                    and prescription.load.kind == "calibration"
                    and exercise.kg_loadable
                    and rir_applies(prescription, exercise)
                ):
                    prescription.rir = DEFAULT_CALIBRATION_RIR
    return fired


def _kg_text(kg: float) -> str:
    return f"{kg:g}"


def load_label(load: Load, exercise: Exercise | None, lang: str) -> str:
    """The explicit load (A§6.4 step 5): a kg number ("each" when the catalog `load_unit` is
    `per_implement`, A§4.4), "bodyweight", or a plain calibration ("start light, log what
    you used"). No stimulus: `prescription_load_label` adds it."""
    if load.kind == "kg":
        assert load.kg is not None  # Load's own validator guarantees this
        if exercise is not None and exercise.load_unit == "per_implement":
            return t("load.per_implement", lang, kg=_kg_text(load.kg))
        return t("plan.load_kg", lang, kg=_kg_text(load.kg))
    if load.kind == "bodyweight":
        return t("plan.load_bodyweight", lang)
    return t("plan.load_calibration", lang)


def prescription_load_label(
    prescription: Prescription,
    exercise: Exercise | None,
    lang: str,
    *,
    section: str = "plan",
    role: BlockRole | None = None,
) -> str:
    """The load as the plan view (`section="plan"`) or a workout block (`"train"`) says it:

    - a calibration load from a pasted plan: its declared kg as the hint (M8b);
    - a calibration load: how to pick it for this stimulus (reps in reserve, a fast load, a
      carry held upright, a pace), never a catalog weight;
    - a kg or bodyweight load: the number, with reps in reserve where they apply, or the
      speed cue for power, speed and agility work — not in a warm-up, conditioning or
      mobility block (`role`), where the same drill is easy or done for capacity.
    """
    load = prescription.load
    mode = mode_of(prescription, exercise)
    rir = prescription.rir if rir_applies(prescription, exercise) else None
    if load.kind == "calibration":
        if prescription.declared_kg is not None:
            declared = load_label(Load(kind="kg", kg=prescription.declared_kg), exercise, lang)
            return t(f"{section}.load_calibration_declared", lang, declared=declared)
        if rir is not None:
            return t(f"{section}.load_calibration_rir", lang, rir=rir)
        key = _CALIBRATION_KEYS.get(mode)
        if key is not None:
            return t(f"{section}.{key}", lang)
        return t(f"{section}.load_calibration", lang)
    label = load_label(load, exercise, lang)
    if rir is not None:
        return f"{label}, {t('plan.rir', lang, rir=rir)}"
    if mode == IntensityMode.SPEED and role not in _NO_SPEED_CUE_ROLES:
        key = "plan.speed_cue" if prescription.measure == Measure.REPS else "plan.speed_cue_time"
        return f"{label}, {t(key, lang)}"
    return label
