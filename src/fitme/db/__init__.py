"""The only place SQL text exists (A§4.6): connection setup, migrations, controllers and
selectors. Everything outside `db/` calls into these modules; nothing outside `db/` writes
`execute()`/`executemany()`/`executescript()` calls or SQL string literals (enforced by
`tests/unit/test_sql_containment.py`).
"""
