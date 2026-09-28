"""M9 review fix B3: `fitme serve` must shut down cleanly on SIGTERM (and SIGINT) — exit 0,
the bot polling stopped, and the database closed, in that order. A real subprocess and a real
OS signal, since the bug (uvicorn's `capture_signals` re-raising the signal with the
*original* handler restored) only reproduces through the actual signal machinery, not by
cancelling an `asyncio.Task` in-process.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest

from fitme.db.connection import open_database
from fitme.db.migrate import migrate

_SCRIPT = """
import asyncio
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher

from fitme.cli import commands
from fitme.config.settings import Settings
from fitme.log import configure_logging

configure_logging()


async def fake_start_polling(self, *bots, **kwargs):
    await asyncio.Event().wait()


async def fake_register_commands(bot):
    return None


async def fake_me(self):
    # M10 review (B4): serve_async's own bot.me() startup preflight would otherwise be a real
    # call to the Telegram API here — never allowed in a test.
    return None


Dispatcher.start_polling = fake_start_polling
commands.register_commands = fake_register_commands
Bot.me = fake_me

settings = Settings(
    telegram_bot_token="123456:TEST-token-for-unit-tests-only",
    db_path=Path(sys.argv[1]),
    web_base_url="https://fit.example.org",
    secret_key="k" * 32,
    web_host="127.0.0.1",
    web_port=int(sys.argv[2]),
)

exit_code = asyncio.run(commands.serve_async(settings))
print(f"EXIT_CODE={exit_code}", flush=True)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _log_messages(stdout: bytes) -> list[str]:
    messages: list[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message")
        if isinstance(message, str):
            messages.append(message)
    return messages


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_serve_shuts_down_cleanly_on_signal(tmp_path: Path, sig: signal.Signals) -> None:
    db_path = tmp_path / "fitme.db"
    database = await open_database(db_path)
    await migrate(database)
    await database.close()

    port = _free_port()
    script_path = tmp_path / "run_serve.py"
    script_path.write_text(_SCRIPT, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        str(db_path),
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # Wait for the web server to come up.
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                if proc.returncode is not None:
                    break
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/", timeout=1)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("web server never came up")

        proc.send_signal(sig)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            pytest.fail("serve_async did not exit after the signal")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert proc.returncode == 0, (
        f"exit code {proc.returncode}\nstdout:\n{stdout.decode(errors='replace')}\n"
        f"stderr:\n{stderr.decode(errors='replace')}"
    )
    messages = _log_messages(stdout)
    assert "bot polling stopped" in messages
    assert "database closed" in messages
    assert messages.index("bot polling stopped") < messages.index("database closed")

    # The database file is still a valid, uncorrupted SQLite file after the clean shutdown.
    reopened = await open_database(db_path)
    try:
        async with reopened.read() as conn, conn.execute("PRAGMA integrity_check") as cursor:
            rows = [tuple(row) for row in await cursor.fetchall()]
            assert rows == [("ok",)]
    finally:
        await reopened.close()
