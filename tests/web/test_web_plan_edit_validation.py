"""M9 review fix B1/B5: the structured plan-edit form validates every value through the
domain models (never `model_copy(update=...)`, which skips validation entirely). A malformed
or implausible value is a form error — never a 500, never silently saved."""

from __future__ import annotations

import pytest
from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient
from test_web_plans import (
    _SQUAT,
    make_plan,
    seed_completed_session,
    seed_confirmed_plan,
    seed_profile,
)

from fitme.db.connection import Database
from fitme.services import planning


def _form(token: str, **over: str) -> dict[str, str]:
    form = {
        "csrf_token": token,
        "w0_b0_i0_exercise": _SQUAT,
        "w0_b0_i0_sets": "3",
        "w0_b0_i0_reps_min": "5",
        "w0_b0_i0_reps_max": "8",
        "w0_b0_i0_rest": "90",
        "w0_b0_i0_load_kind": "kg",
        "w0_b0_i0_load_kg": "60",
    }
    form.update(over)
    return form


async def _setup(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> tuple[int, str]:
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=3)
    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}/edit")
    return plan_id, get_csrf_token(page.text)


_INVALID_CASES = {
    "sets=999": {"w0_b0_i0_sets": "999"},
    "sets=0": {"w0_b0_i0_sets": "0"},
    "reps_min>max": {"w0_b0_i0_reps_min": "20", "w0_b0_i0_reps_max": "5"},
    "rest=-5": {"w0_b0_i0_rest": "-5"},
    "load_kind=bogus": {"w0_b0_i0_load_kind": "bogus"},
    "kg=abc": {"w0_b0_i0_load_kg": "abc"},
    "kg=-5": {"w0_b0_i0_load_kg": "-5"},
    "kg=inf": {"w0_b0_i0_load_kg": "inf", "confirm_over_cap": "on"},
    "kg=nan": {"w0_b0_i0_load_kg": "nan", "confirm_over_cap": "on"},
    "exercise=unknown": {"w0_b0_i0_exercise": "not_a_real_exercise"},
}


@pytest.mark.parametrize("label", list(_INVALID_CASES))
async def test_invalid_edit_never_500s_and_never_saves(
    label: str, client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    plan_id, token = await _setup(client, db, user_id, sent_codes)
    before = await planning.get_plan_detail(db, user_id, plan_id)
    assert before is not None

    response = await client.post(
        f"/app/plans/{plan_id}/edit",
        data=_form(token, **_INVALID_CASES[label]),
        follow_redirects=False,
    )
    assert response.status_code < 500, f"{label}: got a server error"
    assert response.status_code != 303, f"{label}: was saved (redirected) instead of rejected"

    # The plan detail page still renders (no corrupted data was written), and the saved
    # plan is unchanged from before the bad edit.
    detail_page = await client.get(f"/app/plans/{plan_id}")
    assert detail_page.status_code == 200

    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)  # never raises
    assert snapshot is not None

    after = await planning.get_plan_detail(db, user_id, plan_id)
    assert after is not None
    assert after.version.id == before.version.id  # no new version was written


async def test_kg_on_a_non_kg_loadable_exercise_is_rejected(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    plan_id, token = await _setup(client, db, user_id, sent_codes)
    response = await client.post(
        f"/app/plans/{plan_id}/edit",
        data=_form(token, w0_b0_i0_exercise="pushup"),
        follow_redirects=False,
    )
    assert response.status_code == 200  # re-shown with a blocking error, not saved
    assert response.status_code != 303


async def test_implausibly_large_kg_is_rejected_even_with_confirm_checkbox(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    """B5: the absolute plausibility bound is not overridable by the "confirm over cap"
    checkbox, unlike the weekly cap / ceiling."""
    plan_id, token = await _setup(client, db, user_id, sent_codes)
    response = await client.post(
        f"/app/plans/{plan_id}/edit",
        data=_form(token, w0_b0_i0_load_kg="1000000", confirm_over_cap="on"),
        follow_redirects=False,
    )
    assert response.status_code != 303
    after = await planning.get_plan_detail(db, user_id, plan_id)
    assert after is not None
    for workout in after.plan.workouts:
        for block in workout.blocks:
            for item in block.items:
                if item.load.kg is not None:
                    assert item.load.kg < 1_000_000


async def test_a_valid_edit_still_saves(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    plan_id, token = await _setup(client, db, user_id, sent_codes)
    response = await client.post(
        f"/app/plans/{plan_id}/edit",
        data=_form(token, w0_b0_i0_load_kind="calibration"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    after = await planning.get_plan_detail(db, user_id, plan_id)
    assert after is not None
    assert after.plan.workouts[0].blocks[0].items[0].load.kind == "calibration"
