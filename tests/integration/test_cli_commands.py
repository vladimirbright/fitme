"""End-to-end CLI acceptance for M1: `db upgrade`, `export`, `delete`, `purge`, and the
`serve` pending-migrations refusal, all against a temporary DB path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from fitme.cli import commands
from fitme.cli.main import main
from fitme.db.connection import open_database
from fitme.db.migrate import migrate

_REQUIRED_ENV = {
    "FITME_TELEGRAM_BOT_TOKEN": "test-token",
    "FITME_WEB_BASE_URL": "https://fit.example.org",
    "FITME_SECRET_KEY": "test-secret-key-0123456789abcdef",
    # M4/B3: `validate_startup` (called by `serve` and `llm eval`) now checks that the
    # configured provider's API key env var is present. The default tiers all resolve to
    # `anthropic:...` models, so this is "required" the same way the three above are.
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
    yield


async def _seed_a_user(db_path: Path) -> None:
    from fitme.db.connection import open_database
    from fitme.db.controllers.users import insert_user

    db = await open_database(db_path)
    try:
        async with db.transaction() as conn:
            await insert_user(conn, language="en", timezone=None)
    finally:
        await db.close()


def test_db_upgrade_works_against_a_temporary_db_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "fitme.db"
    monkeypatch.setenv("FITME_DB_PATH", str(db_path))

    exit_code = main(["db", "upgrade"])

    assert exit_code == 0
    assert db_path.exists()

    # A second run is a no-op, not an error.
    assert main(["db", "upgrade"]) == 0


def test_serve_refuses_when_database_is_not_initialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "fitme.db"
    monkeypatch.setenv("FITME_DB_PATH", str(db_path))

    exit_code = main(["serve"])

    assert exit_code == 1
    assert "not initialized" in capsys.readouterr().err
    assert not db_path.exists()  # must not create a db file just to say this


def test_serve_refuses_while_migrations_are_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "fitme.db"
    monkeypatch.setenv("FITME_DB_PATH", str(db_path))
    # Create the file, but don't apply any migration, so it exists but is pending.
    db_path.touch()

    exit_code = main(["serve"])

    assert exit_code == 1
    assert "pending migrations" in capsys.readouterr().err

    # Once migrations are applied, `serve` falls through to `commands.serve_async`, which
    # opens a real Bot and starts long polling against the real Telegram API (M5) — out of
    # scope for this no-network test; `tests/bot/` covers the dispatcher and handlers with a
    # mocked Bot session instead.
    assert main(["db", "upgrade"]) == 0


def test_db_upgrade_reports_a_friendly_error_for_a_missing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "no-such-directory" / "fitme.db"))

    exit_code = main(["db", "upgrade"])

    assert exit_code == 1
    assert "directory exists" in capsys.readouterr().err


def test_export_reports_no_user_before_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))
    assert main(["db", "upgrade"]) == 0

    exit_code = main(["export", "--out", str(tmp_path / "export.json")])

    assert exit_code == 1
    assert "No user configured" in capsys.readouterr().err


def test_export_writes_every_table_once_a_user_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "fitme.db"
    monkeypatch.setenv("FITME_DB_PATH", str(db_path))
    assert main(["db", "upgrade"]) == 0

    asyncio.run(_seed_a_user(db_path))

    out_path = tmp_path / "export.json"
    exit_code = main(["export", "--out", str(out_path)])

    assert exit_code == 0
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data["users"]) == 1

    # The export file holds health data (A§5): it must not be world/group readable.
    mode = stat.S_IMODE(out_path.stat().st_mode)
    assert mode == 0o600


def test_export_fixes_permissions_on_an_existing_world_readable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """os.open()'s mode only applies when it creates the file; overwriting a pre-existing
    0644 file must still end up 0600 (A§5)."""
    db_path = tmp_path / "fitme.db"
    monkeypatch.setenv("FITME_DB_PATH", str(db_path))
    assert main(["db", "upgrade"]) == 0
    asyncio.run(_seed_a_user(db_path))

    out_path = tmp_path / "export.json"
    out_path.write_text("stale", encoding="utf-8")
    out_path.chmod(0o644)

    exit_code = main(["export", "--out", str(out_path)])

    assert exit_code == 0
    mode = stat.S_IMODE(out_path.stat().st_mode)
    assert mode == 0o600


def test_delete_refuses_without_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))
    assert main(["db", "upgrade"]) == 0

    assert main(["delete"]) == 1


def test_purge_runs_against_an_upgraded_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))
    assert main(["db", "upgrade"]) == 0

    assert main(["purge"]) == 0


def test_llm_eval_refuses_without_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4: `llm eval` never spends money without an explicit `--yes` — this must hold even
    with a fully valid configuration, and without ever resolving/calling a real model."""
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))

    exit_code = main(["llm", "eval"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "spends real money" in output
    assert "Refusing to run without --yes" in output


def test_llm_eval_rejects_an_unknown_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))

    exit_code = main(["llm", "eval", "--agent", "not_a_real_agent", "--yes"])

    assert exit_code == 1
    assert "unknown agent" in capsys.readouterr().out


def test_llm_eval_rejects_invalid_llm_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FITME_DB_PATH", str(tmp_path / "fitme.db"))
    monkeypatch.setenv("FITME_LLM_AGENT_PLAN_GENERATE", "not-a-known-tier-or-model-string")

    exit_code = main(["llm", "eval", "--agent", "plan_generate", "--yes"])

    assert exit_code == 1
    assert "LLM configuration is invalid" in capsys.readouterr().out


def test_missing_settings_report_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FITME_SECRET_KEY", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(["purge"])

    assert exc_info.value.code == 1
    assert "FITME_SECRET_KEY" in capsys.readouterr().err


async def test_retention_once_survives_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M5 (A§3, A§8.4): a single failed retention pass is logged, not raised — the daily loop
    that calls this must survive a transient DB error rather than dying silently."""
    db = await open_database(tmp_path / "fitme.db")
    await migrate(db)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated retention failure")

    monkeypatch.setattr(commands, "run_retention", _boom)

    try:
        with caplog.at_level(logging.ERROR):
            await commands._retention_once(db, chat_retention_days=365)
    finally:
        await db.close()

    assert "retention run failed" in caplog.text
