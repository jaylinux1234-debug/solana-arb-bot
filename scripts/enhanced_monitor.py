#!/usr/bin/env python3
"""Real-time CEX-DEX opportunity visibility from JSONL logs."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

NEAR_MISS_FILE = Path("logs/cex_dex_near_misses.jsonl")
TRADE_FILE = Path("logs/trade_history.jsonl")
POLL_SEC = 8


def _is_cex_cheap_opportunity(data: dict) -> bool:
    """True when scan was in the profitable direction (CEX buy → DEX sell)."""
    reason = str(data.get("reason") or "").lower()
    if "wrong_direction" in reason:
        return False
    if reason in ("env_thresholds", "aggressive_filter", "sanity_reject") or reason.startswith(
        "env_thresholds_"
    ):
        return True
    if reason.startswith("detect_roundtrip") or reason == "insufficient_depth":
        return True
    if reason == "size_below_min_trade":
        return float(data.get("gross_bps") or 0) > 0
    return "cex_cheap" in reason


def _read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    if not path.exists():
        return [], offset
    raw = path.read_bytes()
    if offset > len(raw):
        offset = 0
    chunk = raw[offset:]
    new_offset = len(raw)
    if not chunk:
        return [], new_offset
    text = chunk.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines, new_offset


def _out(msg: str) -> None:
    print(msg, flush=True)


def tail_opportunities() -> None:
    _out("Enhanced Live Monitor - watching for cex_cheap opportunities")
    _out("=" * 80)

    near_offset = NEAR_MISS_FILE.stat().st_size if NEAR_MISS_FILE.exists() else 0
    trade_offset = TRADE_FILE.stat().st_size if TRADE_FILE.exists() else 0
    cex_cheap_total = 0
    seen_keys: set[str] = set()

    while True:
        try:
            new_near, near_offset = _read_new_lines(NEAR_MISS_FILE, near_offset)
            for line in new_near:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _is_cex_cheap_opportunity(data):
                    continue
                key = f"{data.get('timestamp')}|{data.get('reason')}|{data.get('gross_bps')}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                cex_cheap_total += 1
                _out(
                    f"CEX_CHEAP | pair={data.get('pair')} gross={float(data.get('gross_bps', 0)):.1f} "
                    f"net={float(data.get('net_bps', 0)):.1f} ai={data.get('ai_conf', 0)} "
                    f"reason={data.get('reason')}"
                )

            new_trades, trade_offset = _read_new_lines(TRADE_FILE, trade_offset)
            for line in new_trades:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("live_fill"):
                    _out(
                        f"LIVE_FILL | pair={row.get('pair')} gross={row.get('gross_bps')} "
                        f"net={row.get('net_bps')} tx={row.get('tx_sig', '')[:16]}"
                    )
                elif row.get("execution_attempt"):
                    _out(
                        f"ATTEMPT_BLOCKED | pair={row.get('pair')} "
                        f"reason={row.get('block_reason')}"
                    )

            if TRADE_FILE.exists():
                rows = [
                    json.loads(ln)
                    for ln in TRADE_FILE.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
                recent = rows[-100:]
                fills = sum(1 for r in recent if r.get("live_fill"))
                reasons = Counter(
                    r.get("block_reason") or r.get("source") for r in recent if not r.get("live_fill")
                )
                _out(
                    f"Summary | cex_cheap_events={cex_cheap_total} | "
                    f"fills(last100)={fills} | attempts(last100)={len(recent)} | "
                    f"blocks={reasons.most_common(2)}"
                )

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            _out("\nMonitor stopped.")
            break
        except Exception as exc:
            _out(f"Monitor error: {exc}")
            time.sleep(10)


if __name__ == "__main__":
    tail_opportunities()
