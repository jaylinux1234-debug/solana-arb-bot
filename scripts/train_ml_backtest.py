#!/usr/bin/env python3
"""Train LightGBM — backtest mix (default) or real-fills ensemble (``--ensemble``)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("train_ml_backtest")

from src.config.settings import bootstrap_config

try:
    from src.ai.daily_retrainer import DailyMLRetrainPipeline
    from src.ai.trade_history_trainer import TradeHistoryTrainer
    from src.ml.train_real_fills import load_real_fills_dataframe, train_on_real_fills
except ImportError as exc:
    logger.error(
        "ML deps missing (%s). Install: pip install -r requirements/ml.txt",
        exc,
    )
    raise SystemExit(1) from exc

REAL_FILLS_ENSEMBLE_MIN = int(os.getenv("ML_REAL_FILLS_ENSEMBLE_MIN", "30"))
REGIME_ENSEMBLE_MIN = int(os.getenv("ML_REGIME_ENSEMBLE_MIN", "80"))


def _enrich_real_fills_for_regime(trainer: TradeHistoryTrainer) -> "object":
    import pandas as pd

    raw = load_real_fills_dataframe().to_dict("records")
    rows: list[dict] = []
    for row in raw:
        enriched = trainer._enrich_row(
            {
                **row,
                "gross_spread_bps": row.get("gross_bps"),
                "profit_usdc": row.get("realized_usdc"),
                "was_profitable": row.get("success"),
                "source": "live",
            }
        )
        if enriched:
            rows.append(enriched)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in trainer.feature_columns:
        if col not in df.columns:
            df[col] = 0
    return df


async def main_async(
    *,
    min_samples: int,
    ensemble: bool,
    real_fills_only: bool,
) -> int:
    bootstrap_config()
    os.environ.setdefault("ML_TRAIN_REAL_FILLS_ONLY", "true" if (ensemble or real_fills_only) else "false")

    if ensemble or real_fills_only:
        need = REAL_FILLS_ENSEMBLE_MIN
        df = load_real_fills_dataframe()
        logger.info("Real-fill training rows: %s (need >= %s)", len(df), need)
        if len(df) < need:
            logger.error(
                "Need at least %s live fills with on-chain realized_usdc (have %s). "
                "Run live trades first; rows append via log_real_fill → logs/trade_history.jsonl",
                need,
                len(df),
            )
            return 1

        result = train_on_real_fills(min_samples=need)
        if result.get("status") not in ("ok",):
            logger.error("Real-fill train failed: %s", result)
            return 1

        logger.info("Real-fill model metrics: %s", result)

        from src.ai.ensemble_scorer import suggest_ai_min_confidence_floor

        cal = suggest_ai_min_confidence_floor()
        logger.info(
            "Suggested AI_APPROVE_MIN_CONFIDENCE=%s (see backtest_results/ml_confidence_calibration.json)",
            cal.get("suggested_ai_approve_min_confidence"),
        )

        if ensemble and len(df) >= REGIME_ENSEMBLE_MIN:
            trainer = TradeHistoryTrainer()
            regime_df = _enrich_real_fills_for_regime(trainer)
            if len(regime_df) >= REGIME_ENSEMBLE_MIN:
                pipeline = DailyMLRetrainPipeline()
                pipeline.trainer = trainer
                meta = await pipeline.daily_retrain()
                if meta:
                    logger.info("Regime ensemble retrain OK (real fills): %s", meta)
                else:
                    logger.warning("Regime ensemble retrain returned no metadata")
            else:
                logger.info(
                    "Regime ensemble skipped (enriched rows=%s, need %s)",
                    len(regime_df),
                    REGIME_ENSEMBLE_MIN,
                )
        elif ensemble:
            logger.info(
                "Regime ensemble skipped — %s real fills (need %s for regime models)",
                len(df),
                REGIME_ENSEMBLE_MIN,
            )
        return 0

    trainer = TradeHistoryTrainer()
    df = trainer.load_trade_history()
    logger.info(
        "Loaded %s training rows (logs + %s)",
        len(df),
        trainer.backtest_dir,
    )

    if df.empty:
        logger.error("No training data. Run backtest sims first (npm run backtest:cex-dex).")
        return 1

    result = trainer.train_model(min_samples=min_samples)
    if result.get("status") == "insufficient_data":
        logger.error(
            "Need at least %s rows (have %s). Run more sims or add backtest_results/*.jsonl",
            min_samples,
            result.get("n_samples", len(df)),
        )
        return 1

    logger.info("Primary model metrics: %s", result)

    from src.ai.ensemble_scorer import suggest_ai_min_confidence_floor

    cal = suggest_ai_min_confidence_floor()
    logger.info(
        "Suggested AI_APPROVE_MIN_CONFIDENCE=%s (see backtest_results/ml_confidence_calibration.json)",
        cal.get("suggested_ai_approve_min_confidence"),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Train LightGBM on backtest + live history")
    p.add_argument("--min-samples", type=int, default=50)
    p.add_argument(
        "--ensemble",
        action="store_true",
        help=(
            f"Train on real fills only (need >={REAL_FILLS_ENSEMBLE_MIN} live rows); "
            f"regime ensemble if >={REGIME_ENSEMBLE_MIN}"
        ),
    )
    p.add_argument(
        "--real-fills-only",
        action="store_true",
        help=f"Same as --ensemble minimum ({REAL_FILLS_ENSEMBLE_MIN}+ live fills), no regime leg",
    )
    args = p.parse_args()
    raise SystemExit(
        asyncio.run(
            main_async(
                min_samples=args.min_samples,
                ensemble=args.ensemble,
                real_fills_only=args.real_fills_only,
            )
        )
    )


if __name__ == "__main__":
    main()
