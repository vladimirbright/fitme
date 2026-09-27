"""Pure pydantic v2 models and enums (A§4.5). No I/O, no framework imports.

These types are shared by the LLM output schemas, the guards (A§7) and the DB JSON columns
(`plan_versions.body`, `decisions.*`). `guards/` may import only from here and the stdlib
(enforced by `tests/guards/test_guards_import_boundary.py`), so nothing in this package may
import `db/`, `llm/`, `bot/`, `web/`, aiogram, FastAPI or aiosqlite.
"""

from __future__ import annotations
