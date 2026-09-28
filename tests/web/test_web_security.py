"""A§9.2 security headers and cookie flags; A§9.2 startup config check (dev vs. https)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SentCode, extract_code
from httpx import AsyncClient

from fitme.config.settings import Settings, SettingsError
from fitme.services.llm_runtime import LlmRuntime
from fitme.web.app import create_app


async def test_security_headers_present(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.headers["content-security-policy"] == "default-src 'self'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "DENY"


async def test_session_cookie_flags(
    client: AsyncClient, user_id: int, sent_codes: list[SentCode]
) -> None:
    await client.post("/auth/request-code")
    code = extract_code(sent_codes[-1].text)
    response = await client.post("/auth/verify", data={"code": code}, follow_redirects=False)
    set_cookie = response.headers["set-cookie"]
    assert "__Host-fitme_session=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()
    assert "Path=/" in set_cookie


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        telegram_bot_token="123:test",
        db_path=Path("unused.db"),
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


async def _noop_send(chat_id: int, text: str) -> None:
    return None


def test_https_base_url_is_accepted() -> None:
    settings = _settings()
    llm = LlmRuntime(settings=settings)
    create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]


def test_dev_localhost_http_is_accepted() -> None:
    settings = _settings(web_base_url="http://localhost:8080", dev=True)
    llm = LlmRuntime(settings=settings)
    create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]


def test_plain_http_without_dev_is_refused() -> None:
    settings = _settings(web_base_url="http://example.org", dev=False)
    llm = LlmRuntime(settings=settings)
    with pytest.raises(SettingsError):
        create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]


def test_plain_http_localhost_without_dev_is_refused() -> None:
    settings = _settings(web_base_url="http://localhost:8080", dev=False)
    llm = LlmRuntime(settings=settings)
    with pytest.raises(SettingsError):
        create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]


def test_dev_localhost_lookalike_host_is_refused() -> None:
    """M9 review, "ALSO" #4: `http://localhost.evil.example` is not localhost, even though it
    starts with the string "http://localhost" — the dev exception must compare the parsed
    hostname exactly, not do a prefix match."""
    settings = _settings(web_base_url="http://localhost.evil.example", dev=True)
    llm = LlmRuntime(settings=settings)
    with pytest.raises(SettingsError):
        create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]


def test_dev_127_0_0_1_is_accepted() -> None:
    settings = _settings(web_base_url="http://127.0.0.1:8080", dev=True)
    llm = LlmRuntime(settings=settings)
    create_app(db=None, settings=settings, llm=llm, send_code=_noop_send)  # type: ignore[arg-type]
