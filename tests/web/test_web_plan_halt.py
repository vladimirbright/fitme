"""A§6.3/A§7.4: the stop-word scan runs on the web textarea exactly like the bot's, *before*
any LLM call — a hit halts and the model is never invoked."""

from __future__ import annotations

from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient
from test_web_plans import make_plan, seed_confirmed_plan, seed_profile

from fitme.db.connection import Database
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.training import list_open_health_holds


async def test_stop_word_in_revise_textarea_halts_without_calling_the_llm(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))

    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}/revise")
    token = get_csrf_token(page.text)

    response = await client.post(
        f"/app/plans/{plan_id}/revise",
        data={"csrf_token": token, "request_text": "my chest hurts today, sharp pain"},
    )
    assert response.status_code == 200
    assert "halt" in response.text.lower() or "stop" in response.text.lower()

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert holds  # a health_hold was opened
    assert any(d.kind == "session_halt" for d in decisions)
    assert not any(d.kind in ("plan_revise", "plan_generate") for d in decisions)
