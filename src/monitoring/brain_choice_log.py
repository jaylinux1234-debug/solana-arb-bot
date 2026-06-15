"""Append per-cycle brain picks for daily funnel analytics."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_CHOICE_PATH = Path(os.getenv("BRAIN_CHOICE_LOG_PATH", "logs/brain_choices.jsonl"))


def append_brain_choice(record: dict[str, Any]) -> None:
    BRAIN_CHOICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        **record,
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": "brain_choice",
    }
    with BRAIN_CHOICE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
