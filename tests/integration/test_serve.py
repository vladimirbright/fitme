"""`fitme serve` (A§3): the bot and the website run as two tasks in one event loop, and
shutting one down shuts down the other (and the retention loop) too.

`test_serve_concurrently_*` exercises the orchestration helper directly with fakes (fast,
deterministic). `test_serve_async_runs_bot_and_web_together` is the fuller integration test:
a real `serve_async()` against a real temp DB and a real (ephemeral-port) uvicorn server, with
only the Telegram long-polling loop mocked out (no real bot token, no network)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from pathlib import Path

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from fitme.cli import commands
from fitme.config.settings import Settings
from fitme.db.connection import open_database
from fitme.db.migrate import migrate
from fitme.log import configure_logging


async def test_serve_concurrently_runs_both_tasks() -> None:
    polling_started = asyncio.Event()
    web_started = asyncio.Event()

    async def fake_polling() -> None:
        polling_started.set()
        await asyncio.Event().wait()  # runs until cancelled

    async def fake_web() -> None:
        web_started.set()
        await asyncio.Event().wait()

    retention_task = asyncio.create_task(asyncio.Event().wait())

    async def run_briefly() -> None:
        await commands._serve_concurrently(
            polling=fake_polling(), web_serve=fake_web(), retention_task=retention_task
        )

    task = asyncio.create_task(run_briefly())
    await asyncio.wait_for(polling_started.wait(), timeout=1)
    await asyncio.wait_for(web_started.wait(), timeout=1)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert retention_task.cancelled()


async def test_serve_concurrently_stops_polling_when_web_exits() -> None:
    """When the web server exits first (e.g. uvicorn caught SIGINT), the bot polling task is
    cancelled too, and so is the retention loop — graceful shutdown of both (A§3)."""
    polling_cancelled = asyncio.Event()

    async def fake_polling() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            polling_cancelled.set()
            raise

    async def fake_web() -> None:
        return None  # "exits" immediately

    retention_task = asyncio.create_task(asyncio.Event().wait())

    await commands._serve_concurrently(
        polling=fake_polling(), web_serve=fake_web(), retention_task=retention_task
    )

    assert polling_cancelled.is_set()
    assert retention_task.cancelled()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_serve_async_runs_bot_and_web_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = _free_port()
    db_path = tmp_path / "fitme.db"
    database = await open_database(db_path)
    await migrate(database)
    await database.close()

    settings = Settings(
        telegram_bot_token="123456:TEST-token-for-unit-tests-only",
        db_path=db_path,
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
        web_host="127.0.0.1",
        web_port=port,
    )

    polling_calls: list[bool | None] = []

    async def fake_start_polling(self: Dispatcher, *bots: object, **kwargs: object) -> None:
        polling_calls.append(kwargs.get("handle_signals"))  # type: ignore[arg-type]
        await asyncio.Event().wait()

    async def fake_register_commands(bot: object) -> None:
        return None

    async def fake_me(self: Bot) -> None:
        # M10 review (B4): `serve_async` now calls `bot.me()` as a startup preflight, which
        # would otherwise be a real call to the Telegram API — never allowed in a test
        # (AGENTS.md/IMPLEMENTATION_PLAN.md: "No test may call ... the real Telegram API").
        return None

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    monkeypatch.setattr(commands, "register_commands", fake_register_commands)
    monkeypatch.setattr(Bot, "me", fake_me)

    serve_task = asyncio.create_task(commands.serve_async(settings))
    try:
        response = None
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                if serve_task.done():
                    break
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/", timeout=1)
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
        if serve_task.done():
            serve_task.result()  # re-raise whatever crashed it, for a useful failure message
        assert response is not None
        assert response.status_code == 200
        assert polling_calls == [False]
    finally:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(serve_task, timeout=5)


async def test_serve_async_refuses_to_start_on_an_unauthorized_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M10 review (B4): a rejected bot token must fail fast with one clear line and exit 1 —
    not a crash-loop under Docker Compose's `restart: unless-stopped`, and not a traceback."""
    # Rebind the root logger's handler to *this* test's (capsys-patched) stdout: a previous
    # test that called `main()` (which calls this too) may have left a handler bound to its
    # own now-torn-down capsys stream, and a stray `_logger.info(...)` from `serve_async`'s own
    # `finally` blocks writing to that stale stream would otherwise make logging's own error
    # fallback dump an unrelated traceback into *this* test's captured stderr.
    configure_logging()
    port = _free_port()
    db_path = tmp_path / "fitme.db"
    database = await open_database(db_path)
    await migrate(database)
    await database.close()

    settings = Settings(
        telegram_bot_token="123456:TEST-token-for-unit-tests-only",
        db_path=db_path,
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
        web_host="127.0.0.1",
        web_port=port,
    )

    async def fake_me(self: Bot) -> None:
        raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")

    async def unreachable_start_polling(self: Dispatcher, *bots: object, **kwargs: object) -> None:
        pytest.fail("polling must not start when the token was rejected")

    async def unreachable_register_commands(bot: object) -> None:
        pytest.fail("register_commands must not run when the token was rejected")

    monkeypatch.setattr(Bot, "me", fake_me)
    monkeypatch.setattr(Dispatcher, "start_polling", unreachable_start_polling)
    monkeypatch.setattr(commands, "register_commands", unreachable_register_commands)

    exit_code = await commands.serve_async(settings)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "FITME_TELEGRAM_BOT_TOKEN was rejected by Telegram" in err
    assert "Traceback" not in err


async def test_serve_async_continues_when_the_token_check_hits_a_network_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient network error while verifying the token must not stop `fitme serve` —
    aiogram's own polling loop retries those; only a definitive rejection (above) is fatal."""
    port = _free_port()
    db_path = tmp_path / "fitme.db"
    database = await open_database(db_path)
    await migrate(database)
    await database.close()

    settings = Settings(
        telegram_bot_token="123456:TEST-token-for-unit-tests-only",
        db_path=db_path,
        web_base_url="https://fit.example.org",
        secret_key="test-secret-key-0123456789abcdef",
        web_host="127.0.0.1",
        web_port=port,
    )

    async def fake_me(self: Bot) -> None:
        raise TelegramNetworkError(method=GetMe(), message="Network is unreachable")

    async def fake_start_polling(self: Dispatcher, *bots: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    async def fake_register_commands(bot: object) -> None:
        return None

    monkeypatch.setattr(Bot, "me", fake_me)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    monkeypatch.setattr(commands, "register_commands", fake_register_commands)

    with caplog.at_level(logging.WARNING):
        serve_task = asyncio.create_task(commands.serve_async(settings))
        try:
            response = None
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    if serve_task.done():
                        break
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/", timeout=1)
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.05)
            if serve_task.done():
                serve_task.result()
            assert response is not None
            assert response.status_code == 200
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(serve_task, timeout=5)

    assert "could not verify the Telegram bot token" in caplog.text
