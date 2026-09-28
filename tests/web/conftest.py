"""Shared fixtures for website tests (M9): a real migrated temp DB, a `FastAPI` app wired to
it (the OTP send-code call is a fake, in-memory recorder — no network, no real bot), and an
`httpx.AsyncClient` talking to it in-process over `ASGITransport`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fitme.config.settings import Settings
from fitme.db.connection import Database, open_database
from fitme.db.controllers.users import insert_telegram_account, insert_user
from fitme.db.migrate import migrate
from fitme.services.llm_runtime import LlmRuntime
from fitme.web.app import create_app

TEST_CHAT_ID = 555555


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await open_database(tmp_path / "fitme-web-test.db")
    await migrate(database)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST-token-for-unit-tests-only",
        db_path=Path("unused-in-web-tests.db"),
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
    )


@pytest.fixture
async def user_id(db: Database) -> int:
    async with db.transaction() as conn:
        new_user_id = await insert_user(conn, language="en", timezone="Europe/Berlin")
        await insert_telegram_account(
            conn, user_id=new_user_id, telegram_user_id=42, chat_id=TEST_CHAT_ID
        )
    return new_user_id


@dataclass(frozen=True, slots=True)
class SentCode:
    chat_id: int
    text: str


@pytest.fixture
def sent_codes() -> list[SentCode]:
    return []


@pytest.fixture
def app(db: Database, settings: Settings, sent_codes: list[SentCode]) -> FastAPI:
    llm = LlmRuntime(settings=settings)

    async def send_code(chat_id: int, text: str) -> None:
        sent_codes.append(SentCode(chat_id=chat_id, text=text))

    return create_app(db=db, settings=settings, llm=llm, send_code=send_code)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://fit.example.org") as async_client:
        yield async_client


def extract_code(text: str) -> str:
    """The 6-digit OTP inside the full OTP message text (`web.login.otp_message`) —
    `services.webauth.request_login_code` sends more than a bare code (M9 review "ALSO" #2:
    it now says what it's for, how long it's valid, and to ignore it if unsolicited)."""
    match = re.search(r"\d{6}", text)
    assert match is not None, f"no 6-digit code found in {text!r}"
    return match.group()


async def login(client: AsyncClient, sent_codes: list[SentCode]) -> None:
    """Runs the real OTP flow (request-code -> verify) and leaves `client` holding the
    resulting session cookie, for tests that need to be logged in but aren't testing the
    login flow itself."""
    await client.post("/auth/request-code")
    code = extract_code(sent_codes[-1].text)
    response = await client.post("/auth/verify", data={"code": code}, follow_redirects=False)
    assert response.status_code == 303, response.text


def get_csrf_token(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]
