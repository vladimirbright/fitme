"""Canonical enums (A§4.2). Values match the DB `CHECK` constraints in
`db/migrations/0001_init.sql` exactly, string for string: the schema, these enums, the
catalog and the locale files all use the same spellings.

`enum.StrEnum` so `model_dump(mode="json")`/`model_dump_json()` render members as their bare
string value everywhere they're nested (JSON columns, prompts, `decisions.guards_fired`),
without needing `use_enum_values` on every model.
"""

from __future__ import annotations

from enum import StrEnum


class AgeBucket(StrEnum):
    """A§4.2: 7 buckets. There is no under-18 option (AGENTS.md §4: under-18 is out of
    scope)."""

    AGE_18_29 = "18_29"
    AGE_30_39 = "30_39"
    AGE_40_49 = "40_49"
    AGE_50_59 = "50_59"
    AGE_60_69 = "60_69"
    AGE_70_79 = "70_79"
    AGE_80_90 = "80_90"


class WeightBucket(StrEnum):
    """A§4.2: 17 buckets, 10 kg steps."""

    KG_30_39 = "30_39"
    KG_40_49 = "40_49"
    KG_50_59 = "50_59"
    KG_60_69 = "60_69"
    KG_70_79 = "70_79"
    KG_80_89 = "80_89"
    KG_90_99 = "90_99"
    KG_100_109 = "100_109"
    KG_110_119 = "110_119"
    KG_120_129 = "120_129"
    KG_130_139 = "130_139"
    KG_140_149 = "140_149"
    KG_150_159 = "150_159"
    KG_160_169 = "160_169"
    KG_170_179 = "170_179"
    KG_180_189 = "180_189"
    KG_190_200 = "190_200"


class Experience(StrEnum):
    NONE = "none"
    LT_6M = "lt_6m"
    M6_TO_2Y = "6m_2y"
    Y2_TO_5Y = "2y_5y"
    GT_5Y = "gt_5y"


class BarbellExperience(StrEnum):
    YES = "yes"
    SOME = "some"
    NO = "no"


class Preference(StrEnum):
    WEIGHT_TRAINING = "weight_training"
    FULL_BODY = "full_body"
    SPLIT = "split"
    BODYWEIGHT = "bodyweight"
    CONDITIONING = "conditioning"
    MOBILITY = "mobility"


class Focus(StrEnum):
    STRENGTH = "strength"
    MUSCLE = "muscle"
    GENERAL_FITNESS = "general_fitness"
    CONDITIONING = "conditioning"


class Location(StrEnum):
    PUBLIC_GYM = "public_gym"
    STUDIO_GYM = "studio_gym"
    HOME_EQUIPMENT = "home_equipment"
    APARTMENT_NO_EQUIPMENT = "apartment_no_equipment"
    OUTDOOR = "outdoor"


class Equipment(StrEnum):
    DUMBBELLS = "dumbbells"
    BARBELL = "barbell"
    RACK = "rack"
    BENCH = "bench"
    PULL_UP_BAR = "pull_up_bar"
    KETTLEBELL = "kettlebell"
    RESISTANCE_BANDS = "resistance_bands"
    # A§4.2: gym-only. Gym locations have them by default
    # (`domain.catalog.LOCATION_DEFAULT_EQUIPMENT`); the home-equipment questionnaire step
    # doesn't offer them (`domain.catalog.HOME_SELECTABLE_EQUIPMENT`).
    MACHINE = "machine"
    CABLE = "cable"


class ScreeningFlag(StrEnum):
    """A§4.2 `screening_flags.flag`, grouped into red flags, current-injury areas, and the
    free-text catch-all. See `RED_FLAGS`, `AREA_FLAGS` and `OTHER_UNLISTED` below for the
    groupings the guards act on."""

    # Red flags: any "yes" without clearance blocks plan generation (AGENTS.md §2, A§5.6).
    HEART_CONDITION = "heart_condition"
    CHEST_DISCOMFORT = "chest_discomfort"
    DIZZINESS_FAINTING = "dizziness_fainting"
    HIGH_BLOOD_PRESSURE = "high_blood_pressure"
    RECENT_SURGERY = "recent_surgery"
    PREGNANT = "pregnant"
    OTHER_CONDITION_LIMITS = "other_condition_limits"

    # Current-injury areas: gate exercise selection (contraindications) and turn on
    # check-ins for the area (A§5.6).
    NECK_INJURY_CURRENT = "neck_injury_current"
    SHOULDER_INJURY_CURRENT = "shoulder_injury_current"
    ELBOW_WRIST_INJURY_CURRENT = "elbow_wrist_injury_current"
    LOWER_BACK_INJURY_CURRENT = "lower_back_injury_current"
    HIP_INJURY_CURRENT = "hip_injury_current"
    KNEE_INJURY_CURRENT = "knee_injury_current"
    ANKLE_INJURY_CURRENT = "ankle_injury_current"
    HERNIA = "hernia"

    # Free text (A§5.6): never sent to the LLM, needs clearance like a red flag.
    OTHER_UNLISTED = "other_unlisted"


RED_FLAGS: frozenset[ScreeningFlag] = frozenset(
    {
        ScreeningFlag.HEART_CONDITION,
        ScreeningFlag.CHEST_DISCOMFORT,
        ScreeningFlag.DIZZINESS_FAINTING,
        ScreeningFlag.HIGH_BLOOD_PRESSURE,
        ScreeningFlag.RECENT_SURGERY,
        ScreeningFlag.PREGNANT,
        ScreeningFlag.OTHER_CONDITION_LIMITS,
    }
)

AREA_FLAGS: frozenset[ScreeningFlag] = frozenset(
    {
        ScreeningFlag.NECK_INJURY_CURRENT,
        ScreeningFlag.SHOULDER_INJURY_CURRENT,
        ScreeningFlag.ELBOW_WRIST_INJURY_CURRENT,
        ScreeningFlag.LOWER_BACK_INJURY_CURRENT,
        ScreeningFlag.HIP_INJURY_CURRENT,
        ScreeningFlag.KNEE_INJURY_CURRENT,
        ScreeningFlag.ANKLE_INJURY_CURRENT,
        ScreeningFlag.HERNIA,
    }
)

OTHER_UNLISTED = ScreeningFlag.OTHER_UNLISTED

# A§5.6: "other_unlisted" needs clearance exactly like a red flag.
NEEDS_CLEARANCE_FLAGS: frozenset[ScreeningFlag] = RED_FLAGS | {OTHER_UNLISTED}

# A§4.4: catalog `loads_areas` short names, e.g. `knee_injury_current` -> `knee`. `hernia` has
# no catalog "loaded area" of its own (it isn't something a check-in asks about after a
# session); it still contraindicates exercises directly via `contraindicated_by`, so it's in
# `AREA_FLAGS` but deliberately not in this mapping.
AREA_FLAG_TO_LOADS_AREA: dict[ScreeningFlag, str] = {
    ScreeningFlag.NECK_INJURY_CURRENT: "neck",
    ScreeningFlag.SHOULDER_INJURY_CURRENT: "shoulder",
    ScreeningFlag.ELBOW_WRIST_INJURY_CURRENT: "elbow_wrist",
    ScreeningFlag.LOWER_BACK_INJURY_CURRENT: "lower_back",
    ScreeningFlag.HIP_INJURY_CURRENT: "hip",
    ScreeningFlag.KNEE_INJURY_CURRENT: "knee",
    ScreeningFlag.ANKLE_INJURY_CURRENT: "ankle",
}

# The catalog's `loads_areas` (A§4.4) may only use these short names: exactly the values of
# `AREA_FLAG_TO_LOADS_AREA` above, so every area a catalog exercise claims to load has a
# check-in question and a screening flag behind it. `domain.catalog.Exercise` validates
# against this set.
VALID_LOADS_AREAS: frozenset[str] = frozenset(AREA_FLAG_TO_LOADS_AREA.values())

# The inverse of `AREA_FLAG_TO_LOADS_AREA`: a catalog `loads_areas` short name -> the
# screening flag that must be in `contraindicated_by` for any exercise that loads it (A§4.4
# catalog invariant "contraindications cover loaded areas", enforced by
# `domain.catalog.missing_contraindication_coverage` and `fitme catalog check`).
LOADS_AREA_TO_AREA_FLAG: dict[str, ScreeningFlag] = {
    area: flag for flag, area in AREA_FLAG_TO_LOADS_AREA.items()
}


class CheckinAnswer(StrEnum):
    """A§4.2 `checkins.answer`. A check-in row starts `unknown` and is only ever updated by
    an explicit answer (AGENTS.md §2: silence is not consent)."""

    FINE = "fine"
    WORSE = "worse"
    PAIN = "pain"
    UNKNOWN = "unknown"


class WorkoutSessionStatus(StrEnum):
    """A§4.2 `workout_sessions.status` / A§6.5 state machine."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"
    HALTED = "halted"


class HealthHoldReason(StrEnum):
    """A§4.2 `health_holds.reason` / A§6.6 halt path."""

    STOP_WORD = "stop_word"
    CHECKIN_PAIN = "checkin_pain"
    LLM_SAFETY_SIGNAL = "llm_safety_signal"
    PRECHECK_YES = "precheck_yes"


class DecisionKind(StrEnum):
    """A§4.3 `decisions.kind`."""

    PLAN_GENERATE = "plan_generate"
    PLAN_REVISE = "plan_revise"
    SESSION_ADJUST = "session_adjust"
    RESULT_PARSE = "result_parse"
    PROGRESSION = "progression"
    SESSION_HALT = "session_halt"
    REFUSAL = "refusal"
    USER_EDIT = "user_edit"
    SESSION_DELETE = "session_delete"


class RefusalCode(StrEnum):
    """A§4.5 `Refusal.code`. "at least" these (IMPLEMENTATION_PLAN M2): more may be added by
    later milestones as new refusal paths appear."""

    NEEDS_CLEARANCE = "needs_clearance"
    OPEN_HEALTH_HOLD = "open_health_hold"
    NO_SAFE_EXERCISES = "no_safe_exercises"
    NO_SAFE_PLAN = "no_safe_plan"
    OUT_OF_SCOPE = "out_of_scope"
    LLM_UNAVAILABLE = "llm_unavailable"
    PROFILE_INCOMPLETE = "profile_incomplete"
    # B4: a red flag with no explicit yes/no answer (missing or "unknown") blocks planning
    # exactly like an unclearanced "yes" (AGENTS.md §2: silence is not consent).
    SCREENING_INCOMPLETE = "screening_incomplete"


class ExerciseKind(StrEnum):
    """A§4.4 catalog `exercise.kind`."""

    COMPOUND = "compound"
    ISOLATION = "isolation"
    BODYWEIGHT = "bodyweight"
    CARDIO = "cardio"
    MOBILITY = "mobility"


class ExercisePattern(StrEnum):
    """M3 addition: the movement pattern one catalog exercise trains. Drives the "every
    location has a workable full-body set" check (IMPLEMENTATION_PLAN M3): a full-body plan
    needs one of each of the first seven, plus at least one mobility and one conditioning
    option. `ACCESSORY` is the catch-all for isolation/accessory work (curls, raises, calf
    work, ...) that supports a workout without being one of the primary compound patterns."""

    SQUAT = "squat"  # knee-dominant: squat variants, lunges, step-ups
    HINGE = "hinge"  # hip-dominant: deadlifts, hip thrusts, good mornings
    HORIZONTAL_PUSH = "horizontal_push"  # bench/floor press, push-ups
    HORIZONTAL_PULL = "horizontal_pull"  # rows
    VERTICAL_PUSH = "vertical_push"  # overhead press, pike push-up
    VERTICAL_PULL = "vertical_pull"  # pull-ups, or a bodyweight substitute (A§4.4)
    CORE = "core"
    MOBILITY = "mobility"
    CONDITIONING = "conditioning"
    ACCESSORY = "accessory"
