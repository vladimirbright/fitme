"""Website navigation: the left sidebar tabs (Plans, Trainings; the active one follows the
URL prefix), breadcrumbs on every authenticated page with an unlinked current item, the
"Back" links pointing at the logical parent, and none of it on the login page."""

from __future__ import annotations

import re
from datetime import timedelta

from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient
from test_web_plans import make_plan, seed_completed_session, seed_confirmed_plan, seed_profile

from fitme.db.connection import Database

_CRUMB_RE = re.compile(r"<li( aria-current=\"page\")?>(?:<a href=\"([^\"]*)\">)?([^<]*)")
_BACK_RE = re.compile(r'<a class="back" href="([^"]*)">')
_SIDEBAR_RE = re.compile(r'<nav class="sidebar"[^>]*>(.*?)</nav>', re.S)
_TAB_RE = re.compile(r'<a href="([^"]*)"( aria-current="page")?>')


def _crumbs(html: str) -> list[tuple[str, str | None, bool]]:
    """`(label, href or None, is_current)` per breadcrumb item."""
    start = html.index('<nav class="breadcrumbs"')
    end = html.index("</nav>", start)
    return [
        (label.strip(), href or None, bool(current))
        for current, href, label in _CRUMB_RE.findall(html[start:end])
    ]


def _back(html: str) -> str | None:
    match = _BACK_RE.search(html)
    return None if match is None else match.group(1)


def _active_tab(html: str) -> str | None:
    sidebar = _SIDEBAR_RE.search(html)
    assert sidebar is not None, "no sidebar"
    active = [href for href, current in _TAB_RE.findall(sidebar.group(1)) if current]
    assert len(active) <= 1
    return active[0] if active else None


def _tabs(html: str) -> list[str]:
    sidebar = _SIDEBAR_RE.search(html)
    assert sidebar is not None, "no sidebar"
    return [href for href, _current in _TAB_RE.findall(sidebar.group(1))]


async def test_login_page_has_no_sidebar_and_no_breadcrumbs(client: AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert 'class="sidebar"' not in page.text
    assert 'class="breadcrumbs"' not in page.text
    assert 'class="back"' not in page.text
    assert 'class="topbar"' not in page.text


async def test_every_page_has_the_right_trail_back_link_and_active_tab(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    session_id = await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=1)
    await login(client, sent_codes)
    plan_href = f"/app/plans/{plan_id}"

    plans = await client.get("/app/plans")
    assert _crumbs(plans.text) == [("Plans", None, True)]
    assert _back(plans.text) is None
    assert _tabs(plans.text) == ["/app/plans", "/app/trainings"]
    assert _active_tab(plans.text) == "/app/plans"

    detail = await client.get(plan_href)
    assert _crumbs(detail.text) == [("Plans", "/app/plans", False), ("Home strength", None, True)]
    assert _back(detail.text) == "/app/plans"
    assert _active_tab(detail.text) == "/app/plans"

    edit = await client.get(f"{plan_href}/edit")
    assert _crumbs(edit.text) == [
        ("Plans", "/app/plans", False),
        ("Home strength", plan_href, False),
        ("Edit", None, True),
    ]
    assert _back(edit.text) == plan_href
    assert _active_tab(edit.text) == "/app/plans"

    revise = await client.get(f"{plan_href}/revise")
    assert _crumbs(revise.text) == [
        ("Plans", "/app/plans", False),
        ("Home strength", plan_href, False),
        ("Revise", None, True),
    ]
    assert _back(revise.text) == plan_href

    new = await client.get("/app/plans/new")
    assert _crumbs(new.text) == [("Plans", "/app/plans", False), ("New plan", None, True)]
    assert _back(new.text) == "/app/plans"

    trainings = await client.get("/app/trainings")
    assert _crumbs(trainings.text) == [("Trainings", None, True)]
    assert _back(trainings.text) is None
    assert _active_tab(trainings.text) == "/app/trainings"

    training = await client.get(f"/app/trainings/{session_id}")
    crumbs = _crumbs(training.text)
    assert crumbs[0] == ("Trainings", "/app/trainings", False)
    assert crumbs[1][1] is None and crumbs[1][2]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", crumbs[1][0])
    assert _back(training.text) == "/app/trainings"
    assert _active_tab(training.text) == "/app/trainings"

    token = get_csrf_token(trainings.text)
    confirm = await client.post(
        "/app/trainings/delete/confirm",
        data={"csrf_token": token, "ids": str(session_id), "next": plan_href},
    )
    assert confirm.status_code == 200
    assert _crumbs(confirm.text) == [("Trainings", "/app/trainings", False), ("Delete", None, True)]
    assert _back(confirm.text) == plan_href  # the validated `next`
    confirm_default = await client.post(
        "/app/trainings/delete/confirm",
        data={"csrf_token": token, "ids": str(session_id), "next": "https://evil.example/x"},
    )
    assert _back(confirm_default.text) == "/app/trainings"  # an invalid `next` falls back

    stats = await client.get("/app/stats")
    assert _crumbs(stats.text) == [("Stats", None, True)]
    assert _active_tab(stats.text) is None
    assert 'href="/app/stats" aria-current="page"' in stats.text  # the top bar marks it

    account = await client.get("/app/account")
    assert _crumbs(account.text) == [("Account", None, True)]
    assert _active_tab(account.text) is None
    assert 'href="/app/account" aria-current="page"' in account.text


async def test_breadcrumb_labels_are_escaped(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan = make_plan(60.0).model_copy(update={"name": 'A <b>"bold"</b> & plan'})
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, plan)
    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}/edit")
    assert "<b>" not in page.text
    crumbs = _crumbs(page.text)
    assert crumbs[1][0] == "A &lt;b&gt;&#34;bold&#34;&lt;/b&gt; &amp; plan"


async def test_nothing_inline_and_no_history_back(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    """The CSP is `default-src 'self'`: the navigation is CSS + server-rendered HTML only."""
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    session_id = await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=1)
    await login(client, sent_codes)
    for url in (
        "/app/plans",
        f"/app/plans/{plan_id}",
        f"/app/plans/{plan_id}/edit",
        f"/app/plans/{plan_id}/revise",
        "/app/trainings",
        f"/app/trainings/{session_id}",
        "/app/stats",
        "/app/account",
    ):
        page = await client.get(url)
        assert page.status_code == 200, url
        assert "history.back" not in page.text
        assert " style=" not in page.text
        assert "onclick=" not in page.text
        assert page.headers["content-security-policy"] == "default-src 'self'"


async def test_recent_training_in_plan_detail_stays_a_dated_link(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    session_id = await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=1)
    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}")
    assert re.search(rf'<a href="/app/trainings/{session_id}">\d{{4}}-\d{{2}}-\d{{2}}', page.text)
    assert page.text.count(f"/app/trainings/{session_id}") == 1
    _ = timedelta  # keep the import meaningful for readers extending the window checks
