#!/usr/bin/env python3
"""
Daily ML Retraining Pipeline — Automatically retrains on latest trade + market data.
Adapts to changing market regimes (trending, volatile, low-liquidity).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import pandas as pd
from sklearn.metrics import roc_auc_score

from src.ai.trade_history_trainer import TradeHistoryTrainer
from src.monitoring.metrics import record_ml_retrain
from src.utils.market_regime import detect_market_regime_async

logger = logging.getLogger(__name__)

try:
    import schedule
except ImportError:
    schedule = None  # type: ignore[assignment]


class DailyMLRetrainPipeline:
    def __init__(self) -> None:
        self.trainer = TradeHistoryTrainer()
        self.model_dir = Path("backtest_results/models/")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.last_retrain: datetime | None = None

    async def daily_retrain(self) -> dict[str, Any] | None:
        """Full daily retraining with regime-specific models."""
        logger.info("Starting daily ML retraining pipeline...")

        df = self.trainer.load_trade_history()
        if len(df) < 80:
            logger.warning("Not enough data for retraining (%s rows, need 80)", len(df))
            return None

        regime = await detect_market_regime_async()
        logger.info("Market regime detected: %s", regime)

        global_result = self._train_model(df, suffix="global")
        if global_result is None:
            logger.warning("Global model training failed")
            return None

        regime_df = df[df["market_regime"] == regime]
        regime_result = self._train_model(regime_df, suffix=f"regime_{regime}")

        now = datetime.now(UTC).isoformat()
        metadata: dict[str, Any] = {
            "timestamp": now,
            "n_samples": len(df),
            "regime": int(regime),
            "global_auc": global_result.get("auc", 0.0),
            "regime_auc": (regime_result or {}).get("auc", 0.0),
            "last_retrain": now,
        }

        joblib.dump(
            {
                "global": global_result["model"],
                "regime": (regime_result or {}).get("model"),
                "metadata": metadata,
            },
            self.model_dir / "active_ensemble.pkl",
        )

        record_ml_retrain(metadata)
        self.last_retrain = datetime.now(UTC)

        logger.info(
            "Daily retraining completed | regime=%s samples=%s global_auc=%.3f",
            regime,
            len(df),
            metadata["global_auc"],
        )
        return metadata

    def _train_model(
        self,
        df: pd.DataFrame,
        suffix: str,
    ) -> dict[str, Any] | None:
        if len(df) < 30:
            logger.debug("Skipping model_%s — only %s samples", suffix, len(df))
            return None

        for col in self.trainer.feature_columns:
            if col not in df.columns:
                df[col] = 0

        X = df[self.trainer.feature_columns].fillna(0)
        y = df["was_profitable"].astype(int)

        train_data = lgb.Dataset(X, label=y)
        params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 42,
            "learning_rate": 0.03,
            "feature_fraction": 0.75,
            "bagging_fraction": 0.8,
            "verbose": -1,
        }

        model = lgb.train(params, train_data, num_boost_round=300)
        joblib.dump(model, self.model_dir / f"model_{suffix}.pkl")

        auc = 0.0
        try:
            if len(y.unique()) > 1:
                auc = float(roc_auc_score(y, model.predict(X)))
        except Exception as exc:
            logger.debug("AUC eval failed for %s: %s", suffix, exc)

        return {"model": model, "auc": auc}

    def start_scheduler(self) -> None:
        """Run retraining every day at 03:00 UTC (blocking; use in dedicated process)."""
        if schedule is None:
            raise RuntimeError("schedule package required: pip install schedule")

        schedule.every().day.at("03:00").do(self._run_retrain_sync)
        logger.info("Daily ML scheduler started (03:00 UTC)")

        while True:
            schedule.run_pending()
            time.sleep(60)

    def _run_retrain_sync(self) -> None:
        try:
            asyncio.run(self.daily_retrain())
        except Exception as exc:
            logger.error("Scheduled retrain failed: %s", exc, exc_info=True)


async def run_scheduler_async(poll_sec: float = 60.0) -> None:
    """Async-friendly loop: retrain once at start, then every 24h."""
    pipeline = DailyMLRetrainPipeline()
    while True:
        try:
            await pipeline.daily_retrain()
        except Exception as exc:
            logger.error("daily_retrain failed: %s", exc, exc_info=True)
        await asyncio.sleep(max(3600.0, poll_sec))
