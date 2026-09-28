"""A§9.1 `/app/account`: export download, typed-confirmation delete, and session list/revoke.

`services.account.delete_user` already has an exhaustive "every table ends at zero rows"
test (`tests/integration/db/test_account.py`); this only checks that the web route reaches
it correctly and gates it on the typed confirmation.
"""

from __future__ import annotations

import json

from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient

from fitme.db.connection import Database
from fitme.db.selectors.users import get_the_user


async def test_export_downloads_json_with_the_users_data(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await login(client, sent_codes)
    response = await client.get("/app/account/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    data = json.loads(response.text)
    assert data["users"][0]["id"] == user_id


async def test_delete_requires_the_exact_typed_confirmation(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await login(client, sent_codes)
    page = await client.get("/app/account")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/account/delete", data={"csrf_token": token, "confirm": "delete"}
    )
    assert response.status_code == 200
    async with db.read() as conn:
        assert await get_the_user(conn) is not None


async def test_delete_wipes_the_user(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await login(client, sent_codes)
    page = await client.get("/app/account")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/account/delete",
        data={"csrf_token": token, "confirm": "DELETE"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    async with db.read() as conn:
        assert await get_the_user(conn) is None


async def test_sessions_list_and_revoke(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await login(client, sent_codes)
    page = await client.get("/app/account")
    assert page.status_code == 200
    token = get_csrf_token(page.text)

    marker = 'name="id_hash" value="'
    start = page.text.index(marker) + len(marker)
    id_hash = page.text[start : page.text.index('"', start)]

    response = await client.post(
        "/app/account/sessions/revoke",
        data={"csrf_token": token, "id_hash": id_hash},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Revoking the *current* session logs it out.
    again = await client.get("/app/plans", follow_redirects=False)
    assert again.status_code == 303
