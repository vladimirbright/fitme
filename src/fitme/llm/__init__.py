"""pydantic-ai agents, prompt loading, pseudonymization and usage accounting (A§8, M4).

`services/` (later milestones) is the only caller: `bot/`, `web/` and `cli/` never import
`llm/` directly (A§2.1 layering). `llm/` itself never imports `db/` or `services/` — context
building and agent construction are pure; DB reads/writes and orchestration belong to the
caller.
"""

from __future__ import annotations
