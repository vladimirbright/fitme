"""Tests for fitme.log: JSON (A§10, one object per line) and the dev text format."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest

from fitme.log import LOG_FORMAT_ENV, LOG_LEVEL_ENV, JsonFormatter, TextFormatter, configure_logging


def _capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False
    return logger, stream


def test_formats_one_valid_json_object_with_extra_fields() -> None:
    logger, stream = _capturing_logger("fitme.test.json_formatter.basic")

    logger.info("session started", extra={"session_id": 42, "event": "session_start"})

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1

    payload = json.loads(lines[0])  # raises if not valid JSON
    assert payload["message"] == "session started"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "fitme.test.json_formatter.basic"
    assert payload["session_id"] == 42
    assert payload["event"] == "session_start"
    assert "timestamp" in payload
    assert "exc_info" not in payload


def test_renders_exc_info_when_present() -> None:
    logger, stream = _capturing_logger("fitme.test.json_formatter.exc")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("something failed")

    payload = json.loads(stream.getvalue().splitlines()[0])
    assert payload["message"] == "something failed"
    assert "ValueError: boom" in payload["exc_info"]


def test_text_format_is_one_readable_line_with_extras() -> None:
    logger, stream = _capturing_logger("fitme.test.text_formatter")
    logger.handlers[0].setFormatter(TextFormatter())
    logger.info("decision written", extra={"decision_id": 7})
    line = stream.getvalue().splitlines()[0]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6} \(INFO\): decision written decision_id=7",
        line,
    )


@pytest.mark.parametrize(
    ("value", "expected"), [("text", "TextFormatter"), (None, "JsonFormatter")]
)
def test_log_format_env_picks_the_formatter(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    if value is None:
        monkeypatch.delenv(LOG_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(LOG_FORMAT_ENV, value)
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure_logging()
        assert type(root.handlers[0].formatter).__name__ == expected
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)


@pytest.mark.parametrize(("value", "expected"), [("debug", logging.DEBUG), (None, logging.NOTSET)])
def test_log_level_env_sets_only_the_app_loggers(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is None:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    else:
        monkeypatch.setenv(LOG_LEVEL_ENV, value)
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        configure_logging()
        assert logging.getLogger("fitme").level == expected
        assert root.level == logging.INFO
        assert logging.getLogger("aiogram").getEffectiveLevel() == logging.INFO
    finally:
        logging.getLogger("fitme").setLevel(logging.NOTSET)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
