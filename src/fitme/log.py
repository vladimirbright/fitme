"""Logging setup (A§10).

Structured logs go to stdout as one JSON object per line, so they compose well with
journald or any log collector. `FITME_LOG_FORMAT=text` switches to one readable line per
record instead ("2026-10-15 14:03:12.123456 (INFO): message key=value"), for a dev console
(`make serve` sets it). `FITME_LOG_LEVEL` (e.g. `DEBUG`) sets the level of the app's own
`fitme.*` loggers only, leaving third-party libraries at INFO; `make serve` sets `DEBUG` for
local runs. Callers must never pass health values, message text or
Telegram ids as the log message or as `extra` fields at INFO level; log ids and event names
instead.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes every stdlib LogRecord carries. Anything else on a record came from the
# caller's `extra=` dict and is folded into the JSON payload.
_STANDARD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders one LogRecord as one JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Renders one LogRecord as "timestamp (LEVEL): message", local time, then any `extra`
    fields as `key=value` and the traceback on the following lines."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")
        line = f"{timestamp} ({record.levelname}): {record.getMessage()}"
        extras = [
            f"{key}={value}" for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
        ]
        if extras:
            line = f"{line} {' '.join(extras)}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


LOG_FORMAT_ENV = "FITME_LOG_FORMAT"
LOG_LEVEL_ENV = "FITME_LOG_LEVEL"


def configure_logging(level: int = logging.INFO) -> None:
    """Install a single stream handler on the root logger, replacing any existing ones: JSON,
    or text when `FITME_LOG_FORMAT=text`; `FITME_LOG_LEVEL` sets the `fitme` logger's level
    (unset: inherits `level`; an unknown name raises). Read from the environment directly,
    since logging is set up before the settings are loaded (and `fitme` commands that need
    no settings log too)."""
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream=sys.stdout)
    text = os.environ.get(LOG_FORMAT_ENV, "json").strip().lower() == "text"
    handler.setFormatter(TextFormatter() if text else JsonFormatter())
    root.addHandler(handler)
    app_level = os.environ.get(LOG_LEVEL_ENV, "").strip().upper()
    logging.getLogger("fitme").setLevel(app_level or logging.NOTSET)
