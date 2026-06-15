#!/usr/bin/env python3
"""
Advanced AI Fine-Tuning System — Uses live + backtest trade history to improve win rate.
Trains LightGBM on spread, volatility, time-of-day, etc. (logs/ + backtest_results/).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import accuracy_score, precision_score, roc_auc_score
from sklearn.model_selection import train_test_split

from src.config.settings import get_settings
from src.monitoring.metrics import record_ai_model_update

logger = logging.getLogger(__name__)


class TradeHistoryTrainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.model_path = Path("backtest_results/ai_model_v2.pkl")
        self.history_path = Path("logs/trade_history.jsonl")
        self.backtest_dir = Path(
            getattr(self.settings, "BACKTEST_RESULTS_DIR", None)
            or "backtest_results"
        )
        self.model = None
        if self.model_path.is_file():
            try:
                self.model = joblib.load(self.model_path)
            except Exception:
                self.model = None
        self.near_miss_path = Path(
            os.getenv("CEX_DEX_NEAR_MISS_LOG_PATH", "logs/cex_dex_near_misses.jsonl")
        )
        self.feature_columns = [
            "gross_spread_bps",
            "volatility_bps",
            "cex_depth_util",
            "jupiter_impact_pct",
            "jupiter_route_hops",
            "cex_bid_ask_spread_bps",
            "time_of_day",
            "day_of_week",
            "inventory_sol",
            "pnl_last_24h",
            "win_streak",
            "market_regime",
        ]

    def _enrich_row(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        try:
            ts = trade.get("timestamp")
            if ts is None:
                ts = datetime.utcnow().isoformat()
            trade = dict(trade)
            trade["timestamp"] = pd.to_datetime(ts)
            trade["time_of_day"] = trade["timestamp"].hour + trade["timestamp"].minute / 60
            trade["day_of_week"] = trade["timestamp"].weekday()
            if "was_profitable" not in trade:
                profit = trade.get("profit_usdc", trade.get("pnl_usdc", 0))
                trade["was_profitable"] = bool(
                    trade.get("was_profitable", float(profit or 0) > 0)
                )
            if "gross_spread_bps" not in trade and "gross_bps" in trade:
                trade["gross_spread_bps"] = trade["gross_bps"]
            for key, default in (
                ("jupiter_route_hops", 0),
                ("cex_bid_ask_spread_bps", 0.0),
                ("cex_depth_util", 0.5),
                ("jupiter_impact_pct", 0.0),
                ("volatility_bps", 85.0),
            ):
                trade.setdefault(key, default)
            trade["market_regime"] = self._classify_regime(trade)
            return trade
        except Exception as exc:
            logger.debug("skip trade row: %s", exc)
            return None

    def _load_jsonl_file(self, path: Path, data: list[dict[str, Any]]) -> None:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    enriched = self._enrich_row(row)
                    if enriched:
                        data.append(enriched)

    def _load_json_file(self, path: Path, data: list[dict[str, Any]]) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows: list[Any]
        if isinstance(raw, list):
            rows = raw
        elif isinstance(raw, dict):
            rows = raw.get("trades") or raw.get("results") or raw.get("data") or []
            if not rows and "gross_spread_bps" in raw:
                rows = [raw]
        else:
            return
        for row in rows:
            if isinstance(row, dict):
                enriched = self._enrich_row(row)
                if enriched:
                    data.append(enriched)

    def _load_csv_file(self, path: Path, data: list[dict[str, Any]]) -> None:
        df = pd.read_csv(path)
        for _, row in df.iterrows():
            enriched = self._enrich_row(row.to_dict())
            if enriched:
                data.append(enriched)

    def load_backtest_history(self) -> pd.DataFrame:
        """Load trades exported under backtest_results/ (jsonl, json, csv)."""
        data: list[dict[str, Any]] = []
        if not self.backtest_dir.is_dir():
            return pd.DataFrame()

        skip_names = {"README.md", "models"}
        for path in sorted(self.backtest_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name in skip_names or "state_snapshots" in path.parts:
                continue
            suffix = path.suffix.lower()
            try:
                if suffix == ".jsonl":
                    self._load_jsonl_file(path, data)
                elif suffix == ".json":
                    self._load_json_file(path, data)
                elif suffix == ".csv":
                    self._load_csv_file(path, data)
            except Exception as exc:
                logger.warning("Failed to load %s: %s", path, exc)
        return pd.DataFrame(data)

    def load_near_miss_history(self) -> pd.DataFrame:
        """Near-misses as negative training rows (failed gate / wrong direction)."""
        data: list[dict[str, Any]] = []
        if self.near_miss_path.is_file():
            self._load_jsonl_file(self.near_miss_path, data)
        for row in data:
            row["was_profitable"] = False
            row.setdefault("profit_usdc", -0.01)
            row.setdefault("source", "near_miss")
        return pd.DataFrame(data) if data else pd.DataFrame()

    def load_trade_history(self, *, include_near_misses: bool = True) -> pd.DataFrame:
        """Load and enrich historical trades (live logs + backtest_results/ + near-misses)."""
        data: list[dict[str, Any]] = []

        if self.history_path.is_file():
            self._load_jsonl_file(self.history_path, data)

        bt = self.load_backtest_history()
        if not bt.empty:
            data.extend(bt.to_dict("records"))

        if include_near_misses:
            nm = self.load_near_miss_history()
            if not nm.empty:
                data.extend(nm.to_dict("records"))

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)
        for col in self.feature_columns:
            if col not in df.columns:
                df[col] = 0
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").drop_duplicates(
                subset=["timestamp", "gross_spread_bps"],
                keep="last",
            )
        return df.reset_index(drop=True)

    def _classify_regime(self, trade: dict) -> int:
        vol = trade.get('volatility_bps', 80)
        if vol > 150: return 2
        if abs(trade.get('gross_spread_bps', 0)) > 80: return 0
        return 1

    def train_model(self, *, min_samples: int = 50) -> dict:
        if os.getenv("ML_TRAIN_REAL_FILLS_ONLY", "").lower() in ("1", "true", "yes", "on"):
            from src.ml.train_real_fills import train_on_real_fills

            return train_on_real_fills(min_samples=min_samples)

        df = self.load_trade_history()
        if len(df) < min_samples:
            msg = f"Not enough trade history for fine-tuning ({len(df)} rows, need {min_samples})"
            logger.warning(msg)
            return {"status": "insufficient_data", "n_samples": len(df)}

        X = df[self.feature_columns]
        y = df['was_profitable'].astype(int)

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        train_data = lgb.Dataset(X_train, label=y_train)
        params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'verbose': -1
        }

        model = lgb.train(params, train_data, num_boost_round=200)

        # Evaluate
        y_pred = (model.predict(X_test) > 0.5).astype(int)
        y_prob = model.predict(X_test)
        metrics: dict[str, Any] = {
            "accuracy": accuracy_score(y_test, y_pred),
            "precision": precision_score(y_test, y_pred, zero_division=0),
            "n_samples": len(df),
        }
        if len(set(y_test)) > 1:
            metrics["auc"] = roc_auc_score(y_test, y_prob)
        else:
            metrics["auc"] = 0.5

        # Save
        self.model = model
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, self.model_path)
        
        record_ai_model_update(metrics)
        logger.info(
            "AI model fine-tuned | accuracy=%.1f%% precision=%.1f%% auc=%.3f n=%s",
            metrics["accuracy"] * 100,
            metrics["precision"] * 100,
            metrics["auc"],
            metrics["n_samples"],
        )
        try:
            from src.ai.ensemble_scorer import suggest_ai_min_confidence_floor

            cal = suggest_ai_min_confidence_floor()
            metrics["suggested_ai_approve_min_confidence"] = cal.get(
                "suggested_ai_approve_min_confidence"
            )
        except Exception as exc:
            logger.debug("confidence calibration skipped: %s", exc)
        return metrics

    def predict_confidence(self, signal: dict) -> tuple[float, str]:
        """Return confidence score (0-100) and reasoning"""
        if self.model is None:
            return float(self.settings.AI_APPROVE_MIN_CONFIDENCE), "fallback_rule"

        df = pd.DataFrame([{
            col: signal.get(col, 0) for col in self.feature_columns
        }])
        
        prob = float(self.model.predict(df)[0])
        confidence = int(prob * 100)

        reason = "strong_historical_pattern" if confidence > 85 else "moderate_signal"
        return float(max(50, min(98, confidence))), reason