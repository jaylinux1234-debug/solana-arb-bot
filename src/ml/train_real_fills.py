#!/usr/bin/env python3
"""
Train LightGBM on live fills only (on-chain realized USDC).

Data: ``logs/trade_history.jsonl`` rows with ``realized_usdc`` (+ optional
``logs/win_rate_window.json`` supplement). Excludes simulate/backtest rows.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from lightgbm import LGBMClassifier

logger = logging.getLogger(__name__)

DEFAULT_HISTORY = Path("logs/trade_history.jsonl")
DEFAULT_V2_PNL = Path(os.getenv("V2_PNL_LOG", "logs/v2_pnl.jsonl"))
DEFAULT_WIN_RATE = Path(os.getenv("WIN_RATE_STATE_PATH", "logs/win_rate_window.json"))
DEFAULT_MODEL = Path(os.getenv("ML_REAL_FILLS_MODEL_PATH", "models/arb_model.joblib"))
DEFAULT_META = Path(os.getenv("ML_REAL_FILLS_META_PATH", "models/arb_model_meta.json"))

FEATURE_COLUMNS = ["gross_bps", "size_usdc", "hour_utc", "hops"]
MIN_SAMPLES = int(os.getenv("ML_REAL_FILLS_MIN_SAMPLES", "30"))


def _parse_ts(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    try:
        return pd.to_datetime(value, utc=True).to_pydatetime()
    except Exception:
        return datetime.now(UTC)


def _normalize_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("simulate") is True or raw.get("source") in (
        "simulate",
        "backtest",
        "near_miss",
        "live_blocked",
    ):
        return None

    realized = raw.get("realized_usdc")
    if realized is None:
        profit = raw.get("profit_usdc", raw.get("pnl_usdc"))
        if profit is None and not raw.get("live_fill"):
            return None
        realized = profit
    if realized is None or (isinstance(realized, float) and pd.isna(realized)):
        return None

    size_usdc_early = float(
        raw.get("size_usdc")
        or raw.get("trade_usdc")
        or (int(raw.get("size_usdc_micro") or 0) / 1_000_000.0)
        or 0.0
    )
    net_bps_early = float(raw.get("net_bps") or 0.0)
    realized_f = float(realized)
    # Legacy rows stored CEX gross as realized_usdc — estimate true net from net_bps.
    if (
        size_usdc_early > 0
        and net_bps_early != 0
        and realized_f >= size_usdc_early * 0.5
    ):
        realized = size_usdc_early * net_bps_early / 10_000.0

    ts = _parse_ts(raw.get("timestamp") or raw.get("ts"))
    gross = float(raw.get("gross_bps") or raw.get("gross_spread_bps") or 0.0)
    size_micro = int(raw.get("size_usdc_micro") or 0)
    size_usdc = float(raw.get("size_usdc") or (size_micro / 1_000_000.0 if size_micro else 0.0))
    hops = int(
        raw.get("hops")
        or raw.get("jupiter_route_hops")
        or raw.get("route_hops")
        or 0
    )
    success = raw.get("success")
    if success is None:
        success = float(realized) > 0

    return {
        "trade_id": str(raw.get("trade_id") or ""),
        "gross_bps": gross,
        "size_usdc": size_usdc,
        "hour_utc": ts.hour + ts.minute / 60.0,
        "hops": hops,
        "success": bool(success),
        "realized_usdc": float(realized),
        "net_bps": float(raw.get("net_bps") or 0.0),
        "pair": str(raw.get("pair") or raw.get("pair_label") or "SOL/USDC"),
        "live_fill": True,
        "timestamp": ts.isoformat(),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                norm = _normalize_row(raw)
                if norm:
                    rows.append(norm)
    return rows


def _load_win_rate_supplement(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("win_rate state unreadable: %s", exc)
        return []
    trades = payload.get("trades") if isinstance(payload, dict) else []
    if not isinstance(trades, list):
        return []
    rows: list[dict[str, Any]] = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        norm = _normalize_row(
            {
                **t,
                "live_fill": True,
                "timestamp": t.get("ts"),
            }
        )
        if norm:
            rows.append(norm)
    return rows


def _load_v2_pnl_supplement(path: Path) -> list[dict[str, Any]]:
    """Map v2 P&L rows to training features (net_usdc as realized)."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            net = raw.get("net_usdc")
            if net is None:
                continue
            norm = _normalize_row(
                {
                    "trade_id": raw.get("tx_sig") or raw.get("ts"),
                    "realized_usdc": net,
                    "gross_bps": raw.get("gross_bps"),
                    "net_bps": raw.get("net_bps"),
                    "size_usdc": raw.get("trade_usdc"),
                    "timestamp": raw.get("ts"),
                    "live_fill": True,
                    "source": "v2_pnl",
                }
            )
            if norm:
                rows.append(norm)
    return rows


def load_real_fills_dataframe(
    *,
    history_path: Path | None = None,
    win_rate_path: Path | None = None,
    v2_pnl_path: Path | None = None,
    include_win_rate: bool | None = None,
) -> pd.DataFrame:
    """Live fills with on-chain ``realized_usdc`` only."""
    hist = history_path or DEFAULT_HISTORY
    wr = win_rate_path or DEFAULT_WIN_RATE
    pnl = v2_pnl_path or DEFAULT_V2_PNL
    use_wr = (
        include_win_rate
        if include_win_rate is not None
        else os.getenv("ML_TRAIN_INCLUDE_WIN_RATE", "true").lower()
        in ("1", "true", "yes", "on")
    )
    use_pnl = os.getenv("ML_TRAIN_INCLUDE_V2_PNL", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    rows = _load_jsonl(hist)
    if use_pnl:
        seen = {r.get("trade_id") for r in rows if r.get("trade_id")}
        for row in _load_v2_pnl_supplement(pnl):
            tid = row.get("trade_id")
            if tid and tid in seen:
                continue
            rows.append(row)
    if use_wr:
        seen = {r.get("trade_id") for r in rows if r.get("trade_id")}
        for row in _load_win_rate_supplement(wr):
            tid = row.get("trade_id")
            if tid and tid in seen:
                continue
            rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df[df["realized_usdc"].notna()]
    if "trade_id" in df.columns:
        df = df.drop_duplicates(subset=["trade_id"], keep="last")
    return df.reset_index(drop=True)


def train_on_real_fills(
    *,
    history_path: Path | None = None,
    model_path: Path | None = None,
    min_samples: int | None = None,
) -> dict[str, Any]:
    """Fit ``LGBMClassifier`` on real fills; writes joblib + metadata JSON."""
    df = load_real_fills_dataframe(history_path=history_path)
    need = min_samples if min_samples is not None else MIN_SAMPLES

    if df.empty or len(df) < need:
        msg = f"Not enough real fills ({len(df)} rows, need {need})"
        logger.warning(msg)
        return {"status": "insufficient_data", "n_samples": len(df), "message": msg}

    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0

    X = df[FEATURE_COLUMNS].astype(float)
    y = df["success"].astype(int)

    if y.nunique() < 2:
        logger.warning("Real fills need both wins and losses to train (n=%s)", len(df))
        return {"status": "single_class", "n_samples": len(df), "win_rate": float(y.mean())}

    model = LGBMClassifier(
        n_estimators=int(os.getenv("ML_REAL_FILLS_N_ESTIMATORS", "200")),
        class_weight="balanced",
        random_state=42,
        verbose=-1,
    )
    model.fit(X, y)

    out = model_path or DEFAULT_MODEL
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out)

    meta = {
        "status": "ok",
        "n_samples": len(df),
        "win_rate": round(float(y.mean()), 4),
        "feature_columns": FEATURE_COLUMNS,
        "model_path": str(out),
        "trained_at": datetime.now(UTC).isoformat(),
    }
    meta_path = DEFAULT_META
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info(
        "ML model trained on real fills | n=%s win_rate=%.1f%% -> %s",
        meta["n_samples"],
        meta["win_rate"] * 100.0,
        out,
    )
    print(
        f"ML model trained on real fills. Win rate: {meta['win_rate']:.1%} "
        f"(n={meta['n_samples']}) -> {out}"
    )

    try:
        from src.monitoring.metrics import record_ai_model_update

        record_ai_model_update(
            {
                "source": "real_fills",
                "n_samples": meta["n_samples"],
                "win_rate": meta["win_rate"],
            }
        )
    except Exception:
        pass

    return meta


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    train_on_real_fills()
