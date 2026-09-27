"""Write-side data access (INSERT/UPDATE/DELETE), one module per aggregate (A§4.6).

Every function takes the connection first and has typed arguments; controllers never open a
connection or commit (services own transactions via `db.connection.Database.transaction()`).
Append-only tables (`plan_versions`, `decisions`, `decision_outcomes`) expose insert
functions only.
"""
