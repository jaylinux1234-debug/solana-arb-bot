#!/usr/bin/env python3
"""Analyze live fills: win rate by pair from ``logs/trade_history.jsonl`` (or ``logs/trades.jsonl``)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

MIN_FILLS_FOR_ML = 30


def _load_fills() -> tuple[pd.DataFrame, Path]:
    for path in (Path("logs/trade_history.jsonl"), Path("logs/trades.jsonl")):
        if not path.is_file():
            continue
        df = pd.read_json(path, lines=True)
        if df.empty:
            continue
        if "success" not in df.columns and "was_profitable" in df.columns:
            df["success"] = df["was_profitable"]
        if "pair" not in df.columns:
            df["pair"] = "SOL/USDC"
        live = df[df.get("live_fill", True) != False] if "live_fill" in df.columns else df
        if "source" in live.columns:
            live = live[~live["source"].isin(["simulate", "backtest", "near_miss"])]
        return live, path
    return pd.DataFrame(), Path("logs/trade_history.jsonl")


def main() -> int:
    df, path = _load_fills()
    if df.empty:
        print(f"No live fills in {path} or logs/trades.jsonl")
        print(f"Need {MIN_FILLS_FOR_ML}+ rows before: npm run train:ml:ensemble")
        return 1

    n = len(df)
    print(f"Source: {path} | rows={n}")
    if "realized_usdc" in df.columns:
        print(f"Total realized USDC: {df['realized_usdc'].sum():.4f}")
    if "gross_bps" in df.columns and "net_bps" in df.columns:
        print(
            f"Avg gross/net bps: {df['gross_bps'].mean():.2f} / {df['net_bps'].mean():.2f}"
        )

    grouped = df.groupby("pair")["success"].agg(["count", "mean", "sum"])
    grouped.columns = ["trades", "win_rate", "wins"]
    print("\nWin rate by pair:")
    print(grouped)
    print(f"\nOverall win rate: {float(df['success'].mean()):.1%} ({int(df['success'].sum())}/{n})")

    if n < MIN_FILLS_FOR_ML:
        print(f"\nNot ready for ML train — need {MIN_FILLS_FOR_ML - n} more fills.")
        return 1

    print(f"\nReady for ML: npm run train:ml:ensemble")
    return 0


if __name__ == "__main__":
    sys.exit(main())
