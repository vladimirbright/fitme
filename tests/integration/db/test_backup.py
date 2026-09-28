"""`db/backup.py` acceptance (M10): an online, consistent backup, mode 0600, and rotation
keeping only the newest N."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fitme.db.backup import BackupError, create_backup
from fitme.db.connection import Database
from fitme.db.controllers.users import insert_user


async def test_backup_writes_a_restorable_copy(db: Database, tmp_path: Path) -> None:
    async with db.transaction() as conn:
        await insert_user(conn, language="en", timezone=None)

    out_dir = tmp_path / "backups"
    result = await create_backup(db, out_dir, keep=14)

    assert result.path.parent == out_dir
    assert result.path.name.startswith("fitme-") and result.path.name.endswith(".db")
    assert result.removed == ()

    # A restorable copy: open it as a plain sqlite3 file (no aiosqlite/event loop needed) and
    # confirm the row backed up is really there.
    conn = sqlite3.connect(result.path)
    try:
        row = conn.execute("SELECT language FROM users").fetchone()
    finally:
        conn.close()
    assert row == ("en",)


async def test_backup_file_is_mode_0600(db: Database, tmp_path: Path) -> None:
    result = await create_backup(db, tmp_path / "backups", keep=14)

    mode = stat.S_IMODE(result.path.stat().st_mode)
    assert mode == 0o600


async def test_backup_rotates_keeping_only_the_newest_n(db: Database, tmp_path: Path) -> None:
    out_dir = tmp_path / "backups"
    out_dir.mkdir()

    # Pre-seed with fake, older-looking backup files (the real filename format sorts
    # lexically in chronological order, so distinct minute-stamps are enough — no need to
    # actually wait a second between real backups).
    stale_names = [f"fitme-2020010{i}-000000.db" for i in range(1, 4)]
    for name in stale_names:
        (out_dir / name).touch()

    # 3 stale + the new one = 4 total; keep=2 removes the 2 oldest (both stale — the new
    # backup's real timestamp sorts newest).
    result = await create_backup(db, out_dir, keep=2)

    remaining = sorted(p.name for p in out_dir.iterdir())
    assert len(remaining) == 2
    assert result.path.name in remaining
    assert {p.name for p in result.removed} == set(stale_names[:2])
    for name in stale_names[:2]:
        assert not (out_dir / name).exists()
    assert (out_dir / stale_names[2]).exists()


async def test_backup_keep_zero_removes_everything_including_the_new_one(
    db: Database, tmp_path: Path
) -> None:
    out_dir = tmp_path / "backups"
    result = await create_backup(db, out_dir, keep=0)

    assert list(out_dir.iterdir()) == []
    assert result.path in result.removed


async def test_backup_ignores_non_backup_files_in_the_output_directory(
    db: Database, tmp_path: Path
) -> None:
    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    (out_dir / "notes.txt").write_text("not a backup", encoding="utf-8")

    result = await create_backup(db, out_dir, keep=14)

    assert (out_dir / "notes.txt").exists()
    assert result.path.exists()


async def test_backup_collision_within_the_same_second_raises_backup_error(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fitme.db.backup as backup_module

    out_dir = tmp_path / "backups"
    out_dir.mkdir()
    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    (out_dir / frozen.strftime("fitme-%Y%m%d-%H%M%S.db")).touch()

    monkeypatch.setattr(backup_module, "now", lambda: frozen)

    with pytest.raises(BackupError):
        await create_backup(db, out_dir, keep=14)
