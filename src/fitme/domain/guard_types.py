"""The verdict type every guard function returns (A§7).

Lives in `domain/`, not `guards/`: it's a cross-layer contract, not guards-internal
plumbing. `GuardVerdict.model_dump()` is what `decisions.guards_fired` stores verbatim
(A§4.3, AGENTS.md §6), and `services/` (later milestones) builds `Refusal`s from it, so both
sides need the same type without `services/` importing anything from `guards/` beyond the
functions it calls.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GuardVerdict(BaseModel):
    """One guard's outcome for one thing it checked.

    `ok=False` means the checked action is blocked. `detail` is a short, machine-readable-ish
    reason (never free text a user typed) that's safe to log verbatim to
    `decisions.guards_fired`.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rule: str  # e.g. "progression.weekly_cap"
    ok: bool
    detail: str
