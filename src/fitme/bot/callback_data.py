"""Callback data factories (A§6.3): short, structured, and validated against the current
state before anything happens. Every handler checks the `step`/`hold_id`/... it carries
against the DB's own current state and answers a stale one with a toast (A§6.3, A§9's
"ignore stale callbacks and answer them with a short toast").
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class SetupChoice(CallbackData, prefix="sc"):
    """A single-choice answer for one step, e.g. an age bucket or a yes/no."""

    step: str
    value: str


class SetupToggle(CallbackData, prefix="st"):
    """Toggle one value in a multi-select step (preferences, equipment, screening areas)."""

    step: str
    value: str


class SetupNav(CallbackData, prefix="sn"):
    """A navigation action that isn't itself an answer: back, next (confirm a multi-select),
    or switch to manual timezone entry."""

    step: str
    action: str  # "back" | "next" | "manual"


class ProfileFix(CallbackData, prefix="pf"):
    """`/profile`'s "Fix" button: jump into the setup flow at the step for one field."""

    field: str


class HoldClear(CallbackData, prefix="hc"):
    hold_id: int


class DeleteConfirm(CallbackData, prefix="dc"):
    action: str  # "start" | "cancel"


class PlanMenu(CallbackData, prefix="pm"):
    """`/plan` list actions (A§6.2): `new`, `list`, or `view`/`default`/`revise`/`archive`
    for one plan (`plan_id` is 0 for the plan-independent actions)."""

    action: str
    plan_id: int


class PlanDraft(CallbackData, prefix="pd"):
    """A draft's buttons (A§6.4 step 5): `confirm`, `change` or `cancel`, referencing the
    draft's decision id (0 when cancelling a pending revision that has no draft yet)."""

    action: str
    decision_id: int


class TrainPick(CallbackData, prefix="tp"):
    """`/train` before a session exists (A§6.5 steps 1-2): `plan` (pick a plan), `workouts`
    (list a plan's workouts, "Pick another") or `workout` (start the precheck for `key`)."""

    kind: str
    plan_id: int
    key: str = ""


class TrainAction(CallbackData, prefix="tr"):
    """Every button of a workout session (A§6.3: tied to the session id, the block index and
    — for per-prescription result entry — the item), so a stale block's button is rejected by
    `services.training` against the session's current state, not trusted."""

    action: str
    session_id: int
    block: int = 0
    item: int = 0
    # The `result_parse` decision a Correct button confirms: an older table's Correct must not
    # apply the newest parse.
    decision_id: int = 0


class CheckinReply(CallbackData, prefix="ci"):
    """A post-workout check-in answer (A§6.5 step 6): one `checkins` row, one explicit
    answer (`fine`/`worse`/`pain`). Answerable later until the next session starts."""

    checkin_id: int
    answer: str
