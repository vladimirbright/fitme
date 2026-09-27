"""aiogram routers, keyboards and middlewares (A§6, M5).

Handlers call only `services/`, never `db/` or `llm/` directly (A§2.1). `app.py` assembles
the `Dispatcher`; `serve.py` (in `cli/`) runs it with long polling.
"""

from __future__ import annotations
