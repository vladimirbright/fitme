"""Tests for fitme.log.JsonFormatter (A§10: one JSON object per line)."""

from __future__ import annotations

import io
import json
import logging

from fitme.log import JsonFormatter


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
