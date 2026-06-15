"""structlog configuration — JSON logs for Loki/Grafana with stdlib bridge."""

from __future__ import annotations

import logging
import os

import structlog


def configure_structlog(*, log_level: int = logging.INFO) -> None:
    """Configure structlog; stdlib ``logging`` records flow through the same processors."""
    use_json = (os.getenv("LOG_FORMAT") or "").strip().lower() in ("json", "structured")
    renderer = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
    )

    root = logging.getLogger()
    root.setLevel(log_level)
    for handler in root.handlers:
        handler.setFormatter(formatter)

    structlog.contextvars.bind_contextvars(
        service=os.getenv("LOG_SERVICE_NAME", "solana-arb-monitor"),
        prompt_version=os.getenv("AI_PROMPT_VERSION", ""),
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
