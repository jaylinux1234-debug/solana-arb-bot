"""Structured JSON log records for Loki / Grafana."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line (Loki-friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.getenv("LOG_SERVICE_NAME", "solana-arb-monitor"),
            "prompt_version": os.getenv("AI_PROMPT_VERSION", ""),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key in ("provider", "strategy", "bundle_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)
