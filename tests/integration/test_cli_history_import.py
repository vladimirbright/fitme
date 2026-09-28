"""`fitme history import PATH [--dry-run]` end to end (M11): dry run writes nothing, a real
import writes, `-` reads standard input (the Docker path), a re-import is a no-op, a
malformed file fails with exit 1 and nothing written, and there is no import before
activation."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from fitme.cli.main import main
from fitme.db.connection import open_database

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history_sample.toml"
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
    yield


async def _seed_a_user(db_path: Path) -> None:
    from fitme.db.controllers.users import insert_user

    db = await open_database(db_path)
    try:
        async with db.transaction() as conn:
            await insert_user(conn, language="en", timezone="Europe/Berlin")
    finally:
        await db.close()


async def _session_count(db_path: Path) -> int:
    db = await open_database(db_path)
    try:
        async with db.read() as conn, conn.execute("SELECT COUNT(*) FROM workout_sessions") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    assert row is not None
    return int(row[0])


def _ready(tmp_path: Path) -> Path:
    db_path = tmp_path / "fitme.db"
    assert main(["db", "upgrade"]) == 0
    asyncio.run(_seed_a_user(db_path))
    return db_path


def test_dry_run_then_import_then_stdin_reimport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _ready(tmp_path)

    assert main(["history", "import", str(_FIXTURE), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run (nothing written): 3 new session(s)" in out
    assert asyncio.run(_session_count(db_path)) == 0

    assert main(["history", "import", str(_FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "Imported: 3 new session(s), 0 duplicate session(s) skipped" in out
    # No profile yet: the plan is reported, not saved; the sessions still import.
    assert "rejected plan 'Old two-day': plans need a complete setup" in out
    assert "Decision " in out
    assert "dumbbell_bench_press: highest imported load 20 kg each" in out
    assert "3 session(s) are newer than your last logged training" in out
    assert asyncio.run(_session_count(db_path)) == 3

    monkeypatch.setattr("sys.stdin", io.StringIO(_FIXTURE.read_text(encoding="utf-8")))
    assert main(["history", "import", "-"]) == 0
    out = capsys.readouterr().out
    assert "0 new session(s), 3 duplicate session(s) skipped" in out
    assert asyncio.run(_session_count(db_path)) == 3


def test_malformed_file_fails_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _ready(tmp_path)
    broken = tmp_path / "broken.toml"
    broken.write_text("[meta]\nversion = 1\n[[session]]\ndate = 2026-01-01\n[[bogus]]\n", "utf-8")

    assert main(["history", "import", str(broken)]) == 1
    err = capsys.readouterr().err
    assert "Import failed, nothing written" in err
    assert "unknown key(s) bogus" in err
    assert asyncio.run(_session_count(db_path)) == 0


def test_missing_file_and_missing_user_fail_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["db", "upgrade"]) == 0
    assert main(["history", "import", str(tmp_path / "nope.toml")]) == 1
    assert "Cannot read" in capsys.readouterr().err

    assert main(["history", "import", str(_FIXTURE)]) == 1
    assert "run `fitme activate` first" in capsys.readouterr().err
