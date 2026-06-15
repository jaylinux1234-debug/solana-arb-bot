#!/usr/bin/env python3
"""Backfill logs/v2_pnl.jsonl from live fills in v2_attempts or trade_history."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNL_LOG = Path(os.getenv("V2_PNL_LOG", "logs/v2_pnl.jsonl"))
ATTEMPTS = Path(os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl"))
HISTORY = Path("logs/trade_history.jsonl")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(raw)
    return rows


def _estimate_net_usdc(row: dict) -> float:
    if row.get("net_profit_usdc") is not None:
        return float(row["net_profit_usdc"])
    if row.get("net_usdc") is not None:
        return float(row["net_usdc"])
    trade = float(row.get("trade_usdc") or row.get("size_usdc") or 0)
    net_bps = float(row.get("net_bps") or 0)
    if trade > 0 and net_bps != 0:
        return trade * net_bps / 10_000.0
    realized = float(row.get("realized_usdc") or 0)
    if 0 < realized < trade * 0.5:
        return realized
    return 0.0


def _to_pnl_record(row: dict) -> dict | None:
    tx = str(row.get("tx_sig") or row.get("jupiter_tx_sig") or "").strip()
    if not tx or tx == "test_tx_sig":
        return None
    trade = float(row.get("trade_usdc") or row.get("size_usdc") or 0)
    if trade <= 0:
        return None
    net = _estimate_net_usdc(row)
    spent = float(row.get("usdc_spent_jupiter") or trade)
    received = float(row.get("usdc_received_cex") or (spent + net))
    ts = row.get("ts") or row.get("timestamp") or datetime.now(UTC).isoformat()
    return {
        "ts": ts if isinstance(ts, str) else datetime.now(UTC).isoformat(),
        "net_usdc": round(net, 6),
        "usdc_spent": round(spent, 6),
        "usdc_received": round(received, 6),
        "jito_tip_usdc": float(row.get("jito_tip_usdc") or 0),
        "fees_jupiter": float(row.get("fees_jupiter") or 0),
        "fees_cex": float(row.get("fees_cex") or 0),
        "total_cost_usdc": round(spent, 6),
        "gross_bps": float(row.get("gross_bps") or 0),
        "net_bps": float(row.get("net_bps") or 0),
        "trade_usdc": round(trade, 6),
        "tx_sig": tx,
        "strategy": "dex_cex_reverse",
        "source": "backfill",
    }


def main() -> int:
    existing = _load_jsonl(PNL_LOG)
    seen = {
        str(r.get("tx_sig"))
        for r in existing
        if r.get("tx_sig") and r.get("tx_sig") != "test_tx_sig"
    }

    candidates: list[dict] = []
    for row in _load_jsonl(ATTEMPTS):
        if row.get("live_fill") or row.get("block_reason") == "filled":
            candidates.append(row)
    for row in _load_jsonl(HISTORY):
        if row.get("live_fill") and row.get("strategy") == "dex_cex_reverse":
            candidates.append(row)

    added: list[dict] = []
    for row in candidates:
        rec = _to_pnl_record(row)
        if rec is None:
            continue
        if rec["tx_sig"] in seen:
            continue
        seen.add(rec["tx_sig"])
        added.append(rec)

    # Drop test placeholder rows on rewrite
    kept = [r for r in existing if r.get("tx_sig") != "test_tx_sig"]
    out = kept + added
    if not added and len(kept) == len(existing):
        print("No new P&L rows to backfill.")
        return 0

    PNL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PNL_LOG.open("w", encoding="utf-8") as fh:
        for rec in out:
            fh.write(json.dumps(rec) + "\n")

    print(f"Wrote {len(out)} rows to {PNL_LOG} (+{len(added)} backfilled)")
    for rec in added:
        print(
            f"  {rec['tx_sig'][:16]}… net=${rec['net_usdc']:.4f} "
            f"trade=${rec['trade_usdc']:.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
