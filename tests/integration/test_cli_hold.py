"""`fitme hold clear [--yes]` (A§6.6): the operator's only bypass for a health hold. It lists
the open holds, asks for a typed `CLEAR` unless `--yes` is given, refuses when stdin isn't a
terminal, and is a plain no-op message when nothing is open."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from fitme.cli import commands
from fitme.cli.main import main
from fitme.config.settings import load_settings
from fitme.db.connection import open_database
from fitme.db.controllers.training import insert_health_hold
from fitme.db.controllers.users import insert_user
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.training import list_open_health_holds

_REQUIRED_ENV = {
    "FITME_TELEGRAM_BOT_TOKEN": "test-token",
    "FITME_WEB_BASE_URL": "https://fit.example.org",
    "FITME_SECRET_KEY": "test-secret-key-0123456789abcdef",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("FITME_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))
    assert main(["db", "upgrade"]) == 0
    yield


async def _seed_user_with_holds(db_path: Path, *, holds: int) -> int:
    db = await open_database(db_path)
    try:
        async with db.transaction() as conn:
            user_id = await insert_user(conn, language="en", timezone=None)
            for _ in range(holds):
                await insert_health_hold(
                    conn, user_id=user_id, reason="stop_word", source_session_id=None
                )
    finally:
        await db.close()
    return user_id


async def _state(db_path: Path, user_id: int) -> tuple[int, list[dict[str, object] | None]]:
    """(open hold count, user_report of every hold_clear decision)."""
    db = await open_database(db_path)
    try:
        async with db.read() as conn:
            open_holds = await list_open_health_holds(conn, user_id)
            decisions = await list_decisions_for_user(conn, user_id)
    finally:
        await db.close()
    return len(open_holds), [d.user_report for d in decisions if d.kind == "hold_clear"]


def _db_path() -> Path:
    return Path(os.environ["FITME_DB_PATH"])


def test_hold_clear_with_yes_clears_and_logs(capsys: pytest.CaptureFixture[str]) -> None:
    user_id = asyncio.run(_seed_user_with_holds(_db_path(), holds=2))

    assert main(["hold", "clear", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "2 open hold(s)" in out
    assert "Cleared 2 hold(s)." in out
    open_count, reports = asyncio.run(_state(_db_path(), user_id))
    assert open_count == 0
    assert len(reports) == 2
    assert all(r is not None and r["source"] == "operator_cli" for r in reports)
    assert {r["hold_id"] for r in reports if r is not None} == {1, 2}


def test_hold_clear_refuses_when_not_interactive_and_no_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    user_id = asyncio.run(_seed_user_with_holds(_db_path(), holds=1))
    monkeypatch.setattr("sys.stdin", _NotATty())

    assert main(["hold", "clear"]) == 1

    assert "not an interactive terminal" in capsys.readouterr().err
    assert asyncio.run(_state(_db_path(), user_id)) == (1, [])


def test_hold_clear_refuses_when_the_typed_confirmation_does_not_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_id = asyncio.run(_seed_user_with_holds(_db_path(), holds=1))
    settings = load_settings()

    exit_code = asyncio.run(
        commands.hold_clear(settings, confirmed=False, prompt=lambda _prompt: "yes")
    )

    assert exit_code == 1
    assert "did not match" in capsys.readouterr().err
    assert asyncio.run(_state(_db_path(), user_id)) == (1, [])


def test_hold_clear_clears_after_typing_clear(capsys: pytest.CaptureFixture[str]) -> None:
    user_id = asyncio.run(_seed_user_with_holds(_db_path(), holds=1))
    settings = load_settings()
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "CLEAR"

    exit_code = asyncio.run(commands.hold_clear(settings, confirmed=False, prompt=answer))

    assert exit_code == 0
    assert prompts and "CLEAR" in prompts[0]
    assert "Cleared 1 hold(s)." in capsys.readouterr().out
    open_count, reports = asyncio.run(_state(_db_path(), user_id))
    assert open_count == 0
    assert reports == [{"source": "operator_cli", "hold_id": 1}]


def test_hold_clear_is_a_no_op_message_with_nothing_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_id = asyncio.run(_seed_user_with_holds(_db_path(), holds=0))

    assert main(["hold", "clear"]) == 0  # no confirmation needed: nothing to confirm

    assert "No open holds" in capsys.readouterr().out
    assert asyncio.run(_state(_db_path(), user_id)) == (0, [])


class _NotATty:
    """Stand-in for a piped stdin."""

    def isatty(self) -> bool:
        return False
