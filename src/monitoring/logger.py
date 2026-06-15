"""Central logging setup for the bot process."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from src.monitoring.json_logging import JsonLogFormatter


def _use_structlog() -> bool:
    raw = (os.getenv("USE_STRUCTLOG") or "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def setup_logging(*, level: int = logging.INFO) -> logging.Logger:
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"bot_{datetime.now():%Y%m%d}.log"

    use_json = (os.getenv("LOG_FORMAT") or "").strip().lower() in ("json", "structured")
    if use_json:
        formatter: logging.Formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logging.basicConfig(
        level=level,
        handlers=[file_handler, stream_handler],
        force=True,
    )

    if _use_structlog():
        from src.monitoring.structlog_setup import configure_structlog

        configure_structlog(log_level=level)

    root = logging.getLogger("solana_bot")
    if use_json:
        backend = "structlog+json" if _use_structlog() else "json"
        root.info(
            "Structured logging enabled (%s, LOG_FORMAT=json) — ship %s to Loki/Promtail",
            backend,
            log_file,
        )
    return root
