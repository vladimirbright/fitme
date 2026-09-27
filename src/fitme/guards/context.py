"""`GuardContext`: everything `guards.plan.validate_plan` needs beyond the `Plan` itself
(A§7, IMPLEMENTATION_PLAN M2).

Built by the caller (`services/`, later milestones) from the DB and the profile; guards
themselves never touch the DB or any other I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from fitme.domain.catalog import Catalog
from fitme.domain.enums import CheckinAnswer, Equipment, HealthHoldReason, Location
from fitme.domain.screening import ScreeningFlagState


@dataclass(frozen=True, slots=True)
class GuardContext:
    catalog: Catalog
    flags: Sequence[ScreeningFlagState]
    # `equipment` is what the *service* resolved for this profile, not necessarily a literal
    # copy of `profiles.equipment`: a gym location (`public_gym`/`studio_gym`) implies every
    # catalog `Equipment` value is available there (A§5.1's equipment multi-select is only
    # asked for "home with equipment"), so the service must pass the full equipment set for
    # gym locations, not an empty/partial one, or `plan.equipment_fit` will wrongly reject
    # every barbell/rack exercise at a public gym.
    equipment: frozenset[Equipment]
    location: Location
    sessions_per_week: int
    # The user's currently open health holds (A§4.2 `health_holds`, already filtered by the
    # caller to `cleared_at IS NULL`); any open hold refuses plan generation (A§6.6, B4).
    holds: Sequence[HealthHoldReason] = field(default_factory=tuple)
    # Per-exercise historical max (`selectors.training.historical_max_kg`); an exercise with
    # no entry, or an entry of `None`, means "no logged history".
    history_max_kg: Mapping[str, float | None] = field(default_factory=dict)
    # Per-exercise **current working load**: the prescribed load of the exercise's last
    # completed session, falling back to the active plan version's prescription (A§7). This
    # is the reference an increase is measured against — not the historical max. An exercise
    # with no entry (or `None`) means "no known current load"; `guards.plan` then falls back
    # to `history_max_kg` as the reference, failing closed by still running the increase
    # checks rather than skipping them.
    current_load_kg: Mapping[str, float | None] = field(default_factory=dict)
    # Per-exercise increase deltas applied in the trailing 7 days, read from
    # `decisions.load_changes` across every decision kind (A§9.4: never from `set_logs`, so
    # deleting logs can't reset the cap).
    increases_7d: Mapping[str, Sequence[float]] = field(default_factory=dict)
    # Latest check-in answer per catalog `loads_areas` name (A§4.4); an area with no entry is
    # treated as `unknown` (AGENTS.md §2: silence is not consent).
    checkins: Mapping[str, CheckinAnswer] = field(default_factory=dict)
    # Per-exercise weekly increment cap; falls back to `default_weekly_cap_kg` when an
    # exercise has no entry (AGENTS.md §2: default 2.5 kg, catalog/config may set lower).
    weekly_cap_kg: Mapping[str, float] = field(default_factory=dict)
    default_weekly_cap_kg: float = 2.5
