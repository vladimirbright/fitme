"""Read-side data access (SELECT only), one module per aggregate/read model (A§4.6).

Every function takes the connection first, has typed arguments, and returns a frozen
dataclass from `db.records` (or a plain typed value) — never a raw `sqlite3.Row` or tuple.
"""
