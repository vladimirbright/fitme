"""Safety-invariant guards (AGENTS.md §2, A§7): pure functions over `domain/` objects, no I/O.

Every function here returns a verdict (`domain.guard_types.GuardVerdict`, or
`stop_words.StopHit | None`) and never raises for a business outcome — only for a genuine
programming error. This package imports nothing outside `fitme.domain` and the stdlib
(enforced by `tests/guards/test_guards_import_boundary.py`): no `db/`, `llm/`, `bot/`,
`web/`, aiogram, FastAPI or pydantic-ai, and no bare `pydantic` import either (domain/ owns
that; guards/ only ever imports the already-built types from `fitme.domain.*`).
"""

from __future__ import annotations
