"""Event types for the in-process event bus."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """Canonical bot event categories."""

    CYCLE_START = "cycle_start"
    CYCLE_END = "cycle_end"
    OPPORTUNITY = "opportunity"
    TRADE_EXECUTED = "trade_executed"
    TRADE_FAILED = "trade_failed"
    RISK_BLOCKED = "risk_blocked"
    WEBHOOK_INGEST = "webhook_ingest"
    WEBHOOK_BACKRUN = "webhook_backrun"
    ALERT = "alert"
    RPC_FAILURE = "rpc_failure"


@dataclass(slots=True)
class BotEvent:
    """Payload published on the event bus."""

    kind: EventKind
    source: str = "bot"
    data: dict[str, Any] = field(default_factory=dict)
