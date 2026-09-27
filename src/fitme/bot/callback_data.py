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
