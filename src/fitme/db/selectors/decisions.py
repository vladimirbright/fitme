"""Read-side access for `decisions`, `decision_outcomes` and `llm_calls` (A§4.3)."""

from __future__ import annotations

import json

import aiosqlite

from fitme.db.records import DecisionOutcomeRecord, DecisionRecord, LlmCallRecord

_DECISION_COLUMNS = (
    "id, user_id, kind, prompt_template, prompt_version, model, content_version, "
    "llm_input, user_report, proposal, load_changes, guards_fired, created_at"
)


def _decision_from_row(row: aiosqlite.Row) -> DecisionRecord:
    return DecisionRecord(
        id=row[0],
        user_id=row[1],
        kind=row[2],
        prompt_template=row[3],
        prompt_version=row[4],
        model=row[5],
        content_version=row[6],
        llm_input=None if row[7] is None else json.loads(row[7]),
        user_report=None if row[8] is None else json.loads(row[8]),
        proposal=None if row[9] is None else json.loads(row[9]),
        load_changes=json.loads(row[10]),
        guards_fired=json.loads(row[11]),
        created_at=row[12],
    )


async def get_decision(conn: aiosqlite.Connection, decision_id: int) -> DecisionRecord | None:
    async with conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE id = ?", (decision_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _decision_from_row(row)


async def get_session_halt_decision_for_hold(
    conn: aiosqlite.Connection, hold_id: int
) -> DecisionRecord | None:
    """The `kind=session_halt` decision that created `hold_id` (A§6.6): `services.safety.halt`
    embeds `hold_id` into `user_report` at creation time, so the timezone in effect back then
    can be read back later for the "same day" check, without a schema change (`user_report`
    is the sanctioned free-form JSON column, A§4.3)."""
    async with conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE kind = 'session_halt' "
        "AND json_extract(user_report, '$.hold_id') = ? ORDER BY id DESC LIMIT 1",
        (hold_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _decision_from_row(row)


async def list_decisions_for_user(
    conn: aiosqlite.Connection, user_id: int, *, limit: int = 100
) -> list[DecisionRecord]:
    async with conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_decision_from_row(row) for row in rows]


async def list_decision_outcomes(
    conn: aiosqlite.Connection, decision_id: int
) -> list[DecisionOutcomeRecord]:
    async with conn.execute(
        "SELECT id, decision_id, outcome, created_at FROM decision_outcomes "
        "WHERE decision_id = ? ORDER BY id",
        (decision_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        DecisionOutcomeRecord(
            id=row[0], decision_id=row[1], outcome=json.loads(row[2]), created_at=row[3]
        )
        for row in rows
    ]


async def recent_increase_deltas(
    conn: aiosqlite.Connection, user_id: int, exercise_id: str, *, since: str
) -> list[float]:
    """Positive load-change deltas (kg) applied to `exercise_id` at or after `since`, read
    from `decisions.load_changes` (A§9.4: never from `set_logs`, so deleting training logs
    can't reset the weekly cap; A§7 `progression.check_weekly_increment`'s `increases_7d`).

    Every decision kind is summed (progression, plan_revise, session_adjust, user_edit, ...):
    a load change is a load change regardless of what produced it (A§7 table). Only entries
    with `to_kg > from_kg` are returned — a decrease isn't an "increase" for cap purposes, and
    `check_weekly_increment` also filters defensively, but filtering here too means a caller
    never has to guess whether a negative value might be mixed in.
    """
    async with conn.execute(
        "SELECT load_changes FROM decisions "
        "WHERE user_id = ? AND created_at >= ? AND load_changes != '[]'",
        (user_id, since),
    ) as cursor:
        rows = await cursor.fetchall()

    deltas: list[float] = []
    for (load_changes_json,) in rows:
        for change in json.loads(load_changes_json):
            if change.get("exercise_id") != exercise_id:
                continue
            delta = float(change["to_kg"]) - float(change["from_kg"])
            if delta > 0:
                deltas.append(delta)
    return deltas


async def list_llm_calls_since(conn: aiosqlite.Connection, since: str) -> list[LlmCallRecord]:
    """Used by `/system` (A§6.2) to report token/cost usage over a window."""
    async with conn.execute(
        "SELECT id, decision_id, purpose, model, input_tokens, output_tokens, "
        "cost_estimate_usd, latency_ms, ok, created_at FROM llm_calls "
        "WHERE created_at >= ? ORDER BY id",
        (since,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        LlmCallRecord(
            id=row[0],
            decision_id=row[1],
            purpose=row[2],
            model=row[3],
            input_tokens=row[4],
            output_tokens=row[5],
            cost_estimate_usd=row[6],
            latency_ms=row[7],
            ok=bool(row[8]),
            created_at=row[9],
        )
        for row in rows
    ]


async def recent_increase_deltas_by_exercise(
    conn: aiosqlite.Connection, user_id: int, *, since: str
) -> dict[str, list[float]]:
    """`recent_increase_deltas` for every exercise at once, in one query: the plan flow
    (A§6.4) builds `GuardContext.increases_7d` for the whole catalog, not one exercise at a
    time. Same rules: every decision kind counts, only positive deltas are kept, and the
    source is the append-only `decisions.load_changes` (A§9.4), never `set_logs`."""
    async with conn.execute(
        "SELECT load_changes FROM decisions "
        "WHERE user_id = ? AND created_at >= ? AND load_changes != '[]'",
        (user_id, since),
    ) as cursor:
        rows = await cursor.fetchall()

    deltas: dict[str, list[float]] = {}
    for (load_changes_json,) in rows:
        for change in json.loads(load_changes_json):
            delta = float(change["to_kg"]) - float(change["from_kg"])
            if delta > 0:
                deltas.setdefault(str(change["exercise_id"]), []).append(delta)
    return deltas


async def applied_to_kg_by_exercise(
    conn: aiosqlite.Connection, user_id: int, *, since: str
) -> dict[str, float]:
    """Per exercise, the highest `to_kg` among the `load_changes` entries applied at or after
    `since` (A§7 / A§4.3: every applying decision kind — `plan_confirm`, `progression`,
    `session_adjust`, `user_edit`; drafts store `[]` and so never appear). Feeds
    `guards.context.GuardContext.applied_to_kg_7d`: an increase already applied this week is
    not counted again."""
    async with conn.execute(
        "SELECT load_changes FROM decisions "
        "WHERE user_id = ? AND created_at >= ? AND load_changes != '[]'",
        (user_id, since),
    ) as cursor:
        rows = await cursor.fetchall()

    highest: dict[str, float] = {}
    for (load_changes_json,) in rows:
        for change in json.loads(load_changes_json):
            exercise_id = str(change["exercise_id"])
            to_kg = float(change["to_kg"])
            known = highest.get(exercise_id)
            highest[exercise_id] = to_kg if known is None else max(known, to_kg)
    return highest


# The decision kinds a `/plan` round writes (A§6.4), plus the confirm itself: the current
# draft is the latest of these, so a Confirm/Change for an older one — or for a draft that
# was already confirmed — is stale (A§6.3 "ignore stale callbacks").
_PLAN_ROUND_KINDS = ("plan_generate", "plan_revise", "refusal", "plan_confirm")
_PLAN_ROUND_KINDS_SQL = ", ".join("?" for _ in _PLAN_ROUND_KINDS)


async def get_latest_plan_round_decision(
    conn: aiosqlite.Connection, user_id: int
) -> DecisionRecord | None:
    """The newest `plan_generate`/`plan_revise`/`refusal`/`plan_confirm` decision for this
    user, or `None`. `services.planning.confirm_plan` compares a draft's id against this to
    reject a stale Confirm (a draft superseded by a newer round or already confirmed)."""
    async with conn.execute(
        f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE user_id = ? "
        f"AND kind IN ({_PLAN_ROUND_KINDS_SQL}) ORDER BY id DESC LIMIT 1",
        (user_id, *_PLAN_ROUND_KINDS),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _decision_from_row(row)
