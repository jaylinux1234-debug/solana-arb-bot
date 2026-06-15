"""Logging setup wrapper for the main entrypoint."""

from __future__ import annotations

import logging

from src.config.settings import Settings
from src.monitoring.logger import setup_logging as _setup_monitoring_logging


def setup_logging(settings: Settings | None = None) -> logging.Logger:
    """Configure file + stream handlers (JSON when ``LOG_FORMAT=json``)."""
    return _setup_monitoring_logging()
