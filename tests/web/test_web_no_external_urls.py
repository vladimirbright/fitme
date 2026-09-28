"""A§5/A§9: the website makes no third-party requests. Every rendered page and every static
file must contain no `http://`/`https://` other than `FITME_SOURCE_URL` and the configured
`FITME_WEB_BASE_URL` (which is not itself linked anywhere, but is allowed defensively)."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import SentCode, login
from httpx import AsyncClient
from test_web_plans import make_plan, seed_confirmed_plan, seed_profile

from fitme.config.settings import Settings
from fitme.db.connection import Database

_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "fitme" / "web" / "static"


# The SVG XML namespace URI (`http://www.w3.org/2000/svg`, used by `document.
# createElementNS` in `charts.js`) is a fixed identifier, never a network request or a
# resource load — it is not "external" in the sense this test (and A§5/A§9) cares about.
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def _allowed(url: str, settings: Settings) -> bool:
    if url == _SVG_NAMESPACE:
        return True
    allowed_prefixes = (settings.source_url, settings.web_base_url)
    return any(url == prefix or url.startswith(prefix) for prefix in allowed_prefixes)


def test_static_files_have_no_external_urls(settings: Settings) -> None:
    offenders: list[str] = []
    for path in _STATIC_DIR.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in _URL_RE.findall(text):
            if not _allowed(match, settings):
                offenders.append(f"{path}: {match}")
    assert offenders == []


async def test_rendered_pages_have_no_external_urls(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode], settings: Settings
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))

    login_page = await client.get("/")
    await login(client, sent_codes)
    pages = [
        login_page,
        await client.get("/app/plans"),
        await client.get(f"/app/plans/{plan_id}"),
        await client.get(f"/app/plans/{plan_id}/edit"),
        await client.get("/app/plans/new"),
        await client.get(f"/app/plans/{plan_id}/revise"),
        await client.get("/app/trainings"),
        await client.get("/app/stats"),
        await client.get("/app/account"),
    ]
    offenders: list[str] = []
    for page in pages:
        for match in _URL_RE.findall(page.text):
            if not _allowed(match, settings):
                offenders.append(f"{page.request.url}: {match}")
    assert offenders == []
