"""controllers/selectors for `profiles`, `screening_flags` and `screening_notes` (A§4.2)."""

from __future__ import annotations

from fitme.clock import now
from fitme.db.connection import Database
from fitme.db.controllers.profile import (
    insert_screening_note,
    upsert_profile,
    upsert_screening_flag,
)
from fitme.db.selectors.profile import (
    get_profile,
    get_screening_flag,
    list_screening_flags,
    list_screening_notes,
)


async def test_profile_upsert_supports_partial_then_complete_saves(
    db: Database, user_id: int
) -> None:
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket=None,
            experience=None,
            barbell_experience=None,
            preferences=[],
            location=None,
            equipment=[],
            sessions_per_week=None,
            session_minutes=None,
            focus=None,
            completed_at=None,
        )

    async with db.read() as conn:
        record = await get_profile(conn, user_id)
    assert record is not None
    assert record.age_bucket == "30_39"
    assert record.weight_bucket is None
    assert record.completed_at is None

    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="lt_6m",
            barbell_experience="some",
            preferences=["weight_training", "full_body"],
            location="home_equipment",
            equipment=["dumbbells", "bench"],
            sessions_per_week=3,
            session_minutes=45,
            focus="strength",
            completed_at=now(),
        )

    async with db.read() as conn:
        record = await get_profile(conn, user_id)
    assert record is not None
    assert record.weight_bucket == "80_89"
    assert record.preferences == ["weight_training", "full_body"]
    assert record.equipment == ["dumbbells", "bench"]
    assert record.completed_at is not None


async def test_screening_flag_upsert_is_one_row_per_flag(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn, user_id=user_id, flag="knee_injury_current", value="yes", clearance=None
        )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="heart_condition", value="no", clearance=None
        )

    async with db.read() as conn:
        flags = await list_screening_flags(conn, user_id)
    assert {f.flag for f in flags} == {"knee_injury_current", "heart_condition"}

    # Re-answering the same flag replaces the row rather than adding a second one.
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn, user_id=user_id, flag="knee_injury_current", value="no", clearance=None
        )

    async with db.read() as conn:
        flags = await list_screening_flags(conn, user_id)
        knee = await get_screening_flag(conn, user_id, "knee_injury_current")
    assert len(flags) == 2
    assert knee is not None
    assert knee.value == "no"


async def test_screening_note_insert_and_list(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await insert_screening_note(conn, user_id=user_id, text="old shoulder surgery")

    async with db.read() as conn:
        notes = await list_screening_notes(conn, user_id)
    assert len(notes) == 1
    assert notes[0].text == "old shoulder surgery"
