"""Screening-flag state (A§4.2 `screening_flags`), shaped the way the guards consume it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from fitme.domain.enums import ScreeningFlag


class ScreeningFlagState(BaseModel):
    """One flag's current answer, as read from `screening_flags` (one row per flag)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    flag: ScreeningFlag
    value: Literal["yes", "no", "unknown"]
    clearance: Literal["yes", "no"] | None = None
