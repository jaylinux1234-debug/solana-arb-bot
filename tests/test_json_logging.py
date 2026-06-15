"""Structured JSON logging formatter."""

from __future__ import annotations

import json
import logging

from src.monitoring.json_logging import JsonLogFormatter


def test_json_log_formatter_emits_object() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="oracle tick",
        args=(),
        exc_info=None,
    )
    line = JsonLogFormatter().format(record)
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["message"] == "oracle tick"
    assert "ts" in data
