"""Frozen dataclasses returned by selectors (A§4.6 rule 2: never a raw row or tuple).

`domain/` (the pydantic models used by the LLM, guards and JSON columns) is built in M2.
Until then, JSON columns are deserialized here into plain `dict`/`list` values rather than a
typed domain model; the shape of those dicts matches what M2's models will accept.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: int
    language: str
    timezone: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TelegramAccountRecord:
    id: int
    user_id: int
    telegram_user_id: int
    chat_id: int
    linked_at: str


@dataclass(frozen=True, slots=True)
class ActivationCodeRecord:
    id: int
    code_hash: str
    expires_at: str
    used_at: str | None


@dataclass(frozen=True, slots=True)
class LoginCodeRecord:
    id: int
    user_id: int
    code_hash: str
    expires_at: str
    attempts: int
    used_at: str | None


@dataclass(frozen=True, slots=True)
class WebSessionRecord:
    id_hash: str
    user_id: int
    created_at: str
    last_seen_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    user_id: int
    age_bucket: str | None
    weight_bucket: str | None
    experience: str | None
    barbell_experience: str | None
    preferences: list[str]
    location: str | None
    equipment: list[str]
    sessions_per_week: int | None
    session_minutes: int | None
    focus: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ScreeningFlagRecord:
    id: int
    user_id: int
    flag: str
    value: str
    clearance: str | None
    answered_at: str


@dataclass(frozen=True, slots=True)
class ScreeningNoteRecord:
    id: int
    user_id: int
    text: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HealthHoldRecord:
    id: int
    user_id: int
    reason: str
    source_session_id: int | None
    created_at: str
    cleared_at: str | None


@dataclass(frozen=True, slots=True)
class WorkoutSessionRecord:
    id: int
    user_id: int
    plan_version_id: int
    workout_key: str
    status: str
    current_block: int
    started_at: str | None
    finished_at: str | None
    halt_reason: str | None


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """One past session's outcome for one exercise, as `services.loads` (A§7.3) needs it.

    `load_kg` is the *actual* load logged (`None` when the session's sets for this exercise
    weren't logged with a kg number, e.g. still calibration) — used only for the historical
    max feeding the ceiling guard. `planned_load_kg` is the *prescribed* load; the engine
    progresses from this, not from `load_kg` (A§7.3: logging a heavier weight than prescribed
    must not jump the next prescription, it only raises the ceiling's historical max). See
    `selectors.training.recent_session_outcomes` for how `hit_reps_max`/`below_reps_min` are
    derived from `set_logs`.
    """

    load_kg: float | None
    planned_load_kg: float | None
    hit_reps_max: bool
    below_reps_min: bool


@dataclass(frozen=True, slots=True)
class SetLogRecord:
    id: int
    session_id: int
    exercise_id: str
    set_index: int
    planned_load_kg: float | None
    planned_reps_min: int | None
    planned_reps_max: int | None
    actual_load_kg: float | None
    actual_reps: int | None
    skipped: bool
    rpe: float | None
    source: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CheckinRecord:
    id: int
    user_id: int
    session_id: int | None
    question_key: str
    answer: str
    asked_at: str
    answered_at: str | None


@dataclass(frozen=True, slots=True)
class ChatMessageRecord:
    id: int
    user_id: int
    session_id: int | None
    direction: str
    text: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PlanRecord:
    id: int
    user_id: int
    name: str
    is_default: bool
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class PlanVersionRecord:
    id: int
    plan_id: int
    version: int
    body: dict[str, object]
    origin: str
    decision_id: int
    created_at: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: int
    user_id: int
    kind: str
    prompt_template: str | None
    prompt_version: str | None
    model: str | None
    content_version: str
    llm_input: dict[str, object] | None
    user_report: dict[str, object] | None
    proposal: dict[str, object] | None
    load_changes: list[dict[str, object]]
    guards_fired: list[dict[str, object]]
    created_at: str


@dataclass(frozen=True, slots=True)
class DecisionOutcomeRecord:
    id: int
    decision_id: int
    outcome: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    id: int
    decision_id: int | None
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate_usd: float | None
    latency_ms: int
    ok: bool
    created_at: str
