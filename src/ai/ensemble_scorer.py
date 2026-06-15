"""
CEX-DEX confidence ensemble: heuristic brain + LightGBM (+ optional regime ensemble).

Weights are env-tunable (``ML_ENSEMBLE_HEURISTIC_WEIGHT``, ``ML_ENSEMBLE_LGBM_WEIGHT``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_trainer: Any | None = None
_ensemble_cache: dict[str, Any] | None = None
_real_fills_model: Any | None = None


def get_trainer() -> Any:
    global _trainer
    if _trainer is None:
        try:
            from src.ai.trade_history_trainer import TradeHistoryTrainer

            _trainer = TradeHistoryTrainer()
        except Exception as exc:
            logger.debug("LightGBM trainer unavailable: %s", exc)
            _trainer = False
    return _trainer if _trainer is not False else None


def _load_active_ensemble() -> dict[str, Any] | None:
    global _ensemble_cache
    if _ensemble_cache is not None:
        return _ensemble_cache
    path = Path("backtest_results/models/active_ensemble.pkl")
    if not path.is_file():
        _ensemble_cache = {}
        return None
    try:
        import joblib

        _ensemble_cache = joblib.load(path)
        return _ensemble_cache
    except Exception as exc:
        logger.debug("active_ensemble load failed: %s", exc)
        _ensemble_cache = {}
        return None


def build_signal_features(
    *,
    gross_bps: float,
    net_bps: float,
    size_usdc_micro: int,
    cex_price: float = 0.0,
    jup_price: float = 0.0,
    volatility_bps: float = 85.0,
    cex_depth_util: float = 0.5,
    jupiter_impact_pct: float = 0.0,
    jupiter_route_hops: int = 0,
    cex_bid_ask_spread_bps: float = 0.0,
    inventory_sol: float = 0.0,
    pnl_last_24h: float = 0.0,
    win_streak: int = 0,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Feature dict aligned with ``TradeHistoryTrainer.feature_columns``."""
    ts = timestamp or datetime.now(UTC)
    size_usdc = size_usdc_micro / 1_000_000.0
    if jupiter_impact_pct <= 0 and size_usdc > 0:
        jupiter_impact_pct = min(3.0, size_usdc / 5.0)

    return {
        "gross_spread_bps": float(gross_bps),
        "gross_bps": float(gross_bps),
        "net_bps": float(net_bps),
        "volatility_bps": float(volatility_bps),
        "cex_depth_util": float(cex_depth_util),
        "jupiter_impact_pct": float(jupiter_impact_pct),
        "jupiter_route_hops": int(jupiter_route_hops),
        "cex_bid_ask_spread_bps": float(cex_bid_ask_spread_bps),
        "time_of_day": ts.hour + ts.minute / 60.0,
        "day_of_week": ts.weekday(),
        "inventory_sol": float(inventory_sol),
        "pnl_last_24h": float(pnl_last_24h),
        "win_streak": int(win_streak),
        "cex_price": float(cex_price),
        "jup_price": float(jup_price),
        "size_usdc": size_usdc,
        "size_usdc_micro": size_usdc_micro,
    }


def heuristic_confidence(gross_bps: float, net_bps: float, size_usdc_micro: int) -> float:
    """Fast brain score (no API / model)."""
    base = 65.0
    base += min(25.0, float(net_bps) * 0.6)
    base += min(10.0, size_usdc_micro / 1_000_000.0)
    base += min(8.0, max(0.0, float(gross_bps) - 10) * 0.2)
    return min(98.0, base)


def real_fills_approve_min_proba() -> float:
    return float(os.getenv("ML_REAL_FILLS_APPROVE_MIN_PROBA", "0.68"))


def build_real_fills_features(signal: dict[str, Any]) -> dict[str, float]:
    """Feature row for ``models/arb_model.joblib`` (must match train_real_fills)."""
    import pandas as pd

    ts = signal.get("timestamp")
    if ts is None:
        from datetime import UTC, datetime

        hour_utc = datetime.now(UTC).hour + datetime.now(UTC).minute / 60.0
    else:
        try:
            dt = pd.to_datetime(ts, utc=True)
            hour_utc = dt.hour + dt.minute / 60.0
        except Exception:
            hour_utc = 0.0

    return {
        "gross_bps": float(signal.get("gross_bps") or signal.get("gross_spread_bps") or 0),
        "size_usdc": float(
            signal.get("size_usdc")
            or (int(signal.get("size_usdc_micro") or 0) / 1_000_000.0)
        ),
        "hour_utc": float(signal.get("hour_utc") or signal.get("time_of_day") or hour_utc),
        "hops": float(
            signal.get("hops") or signal.get("jupiter_route_hops") or 0
        ),
    }


def real_fills_predict_proba(signal: dict[str, Any]) -> float | None:
    """P(success) from ``arb_model.joblib``; ``None`` if model unavailable."""
    model = _load_real_fills_model()
    if model is None:
        return None
    import pandas as pd

    from src.ml.train_real_fills import FEATURE_COLUMNS

    row = build_real_fills_features(signal)
    try:
        frame = pd.DataFrame([{c: row[c] for c in FEATURE_COLUMNS}])
        return float(model.predict_proba(frame)[0][1])
    except Exception as exc:
        logger.debug("real_fills predict_proba: %s", exc)
        return None


def passes_real_fills_ml_gate(signal: dict[str, Any]) -> tuple[bool, float | None, str]:
    """
    Hard approve gate: reject when ``predict_proba < ML_REAL_FILLS_APPROVE_MIN_PROBA`` (default 0.68).

    Skips when gate disabled or model not trained yet.
    """
    if os.getenv("ML_REAL_FILLS_APPROVE_GATE", "true").lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return True, None, "gate_disabled"

    prob = real_fills_predict_proba(signal)
    if prob is None:
        return True, None, "no_model"

    floor = real_fills_approve_min_proba()
    if prob < floor:
        logger.info(
            "ML approve REJECT | prob=%.3f < %.2f gross=%.1f size=$%.2f",
            prob,
            floor,
            float(signal.get("gross_bps") or 0),
            float(signal.get("size_usdc") or 0),
        )
        return False, prob, f"ml_prob_below_{floor:.2f}"

    return True, prob, "ml_ok"


def _load_real_fills_model() -> Any | None:
    """``models/arb_model.joblib`` trained via ``src.ml.train_real_fills``."""
    global _real_fills_model
    if _real_fills_model is not None:
        return _real_fills_model if _real_fills_model is not False else None
    path = Path(os.getenv("ML_REAL_FILLS_MODEL_PATH", "models/arb_model.joblib"))
    if not path.is_file():
        _real_fills_model = False
        return None
    try:
        import joblib

        _real_fills_model = joblib.load(path)
        return _real_fills_model
    except Exception as exc:
        logger.debug("real_fills model load failed: %s", exc)
        _real_fills_model = False
        return None


def real_fills_confidence(signal: dict[str, Any]) -> tuple[float | None, str]:
    """LightGBM on live fills only (``arb_model.joblib``)."""
    if os.getenv("ML_REAL_FILLS_ENABLED", "true").lower() not in ("1", "true", "yes", "on"):
        return None, "real_fills_disabled"
    prob = real_fills_predict_proba(signal)
    if prob is None:
        return None, "no_real_fills_model"
    return prob * 100.0, "real_fills_lgbm"


def lgbm_confidence(signal: dict[str, Any]) -> tuple[float, str]:
    """LightGBM primary model (``ai_model_v2.pkl``)."""
    rf_conf, rf_reason = real_fills_confidence(signal)
    if rf_conf is not None:
        return rf_conf, rf_reason

    trainer = get_trainer()
    if trainer is None:
        return heuristic_confidence(
            float(signal.get("gross_bps") or signal.get("spread_bps") or 0),
            float(signal.get("net_bps") or 0),
            int(signal.get("size_usdc_micro") or 0),
        ), "lgbm_unavailable"
    conf, reason = trainer.predict_confidence(signal)
    return float(conf), reason


def regime_ensemble_confidence(signal: dict[str, Any]) -> tuple[float | None, str]:
    """Optional second model from daily retrain ``active_ensemble.pkl``."""
    bundle = _load_active_ensemble()
    if not bundle:
        return None, "no_ensemble"

    import pandas as pd

    trainer = get_trainer()
    if trainer is None:
        return None, "no_trainer"
    regime = int(signal.get("market_regime", trainer._classify_regime(signal)))
    model = (bundle.get("regime") if regime == bundle.get("metadata", {}).get("regime") else None) or bundle.get(
        "global"
    )
    if model is None:
        return None, "ensemble_empty"

    row = {col: signal.get(col, 0) for col in trainer.feature_columns}
    for col in trainer.feature_columns:
        if col not in row:
            row[col] = 0
    try:
        prob = float(model.predict(pd.DataFrame([row]))[0])
        return prob * 100.0, "regime_ensemble"
    except Exception as exc:
        logger.debug("regime ensemble predict: %s", exc)
        return None, "ensemble_error"


def blend_confidence(
    heuristic: float,
    ml: float,
    *,
    ensemble: float | None = None,
) -> float:
    """Weighted blend of brain + ML (+ optional third leg)."""
    w_heur = float(os.getenv("ML_ENSEMBLE_HEURISTIC_WEIGHT", "0.35"))
    w_lgbm = float(os.getenv("ML_ENSEMBLE_LGBM_WEIGHT", "0.50"))
    w_reg = float(os.getenv("ML_ENSEMBLE_REGIME_WEIGHT", "0.15"))

    if ensemble is None:
        total = w_heur + w_lgbm
        if total <= 0:
            return heuristic
        return (heuristic * w_heur + ml * w_lgbm) / total

    total = w_heur + w_lgbm + w_reg
    if total <= 0:
        return heuristic
    return (heuristic * w_heur + ml * w_lgbm + ensemble * w_reg) / total


async def score_opportunity(
    *,
    gross_bps: float,
    net_bps: float,
    size_usdc_micro: int,
    cex_price: float = 0.0,
    jup_price: float = 0.0,
    volatility_bps: float = 85.0,
    extra: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """
    Full ensemble score (0–100) for gating and logging.

    Returns ``(confidence, reason_tag)``.
    """
    sig = build_signal_features(
        gross_bps=gross_bps,
        net_bps=net_bps,
        size_usdc_micro=size_usdc_micro,
        cex_price=cex_price,
        jup_price=jup_price,
        volatility_bps=volatility_bps,
        **(extra or {}),
    )

    ml_ok, ml_prob, ml_gate = passes_real_fills_ml_gate(sig)
    if not ml_ok:
        pct = int(round((ml_prob or 0.0) * 100))
        return float(max(0, min(67, pct))), f"rejected|{ml_gate}"

    trainer = get_trainer()
    sig["market_regime"] = (
        trainer._classify_regime(sig) if trainer is not None else 0
    )

    heur = heuristic_confidence(gross_bps, net_bps, size_usdc_micro)
    ml_conf, ml_reason = lgbm_confidence(sig)
    reg_conf, reg_reason = regime_ensemble_confidence(sig)

    final = blend_confidence(heur, ml_conf, ensemble=reg_conf)
    final = max(50.0, min(98.0, final))

    tag = f"ens(heur={heur:.0f},lgbm={ml_conf:.0f}"
    if reg_conf is not None:
        tag += f",reg={reg_conf:.0f}"
    tag += f")|{ml_reason}|{reg_reason}"

    logger.debug("ML ensemble | %s -> %.1f", tag, final)
    return round(final, 1), tag


def suggest_ai_min_confidence_floor() -> dict[str, Any]:
    """
    After training, suggest ``AI_APPROVE_MIN_CONFIDENCE`` from model precision curve.

    Writes ``backtest_results/ml_confidence_calibration.json``.
    """
    out_path = Path("backtest_results/ml_confidence_calibration.json")
    trainer = get_trainer()
    if trainer is None:
        payload = {
            "suggested_ai_approve_min_confidence": int(
                os.getenv("AI_APPROVE_MIN_CONFIDENCE", "68")
            ),
            "status": "trainer_unavailable",
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    df = trainer.load_trade_history()
    if df.empty or "was_profitable" not in df.columns:
        payload = {
            "suggested_ai_approve_min_confidence": int(
                os.getenv("AI_APPROVE_MIN_CONFIDENCE", "68")
            ),
            "status": "no_data",
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    if trainer.model is None and trainer.model_path.is_file():
        try:
            import joblib

            trainer.model = joblib.load(trainer.model_path)
        except Exception:
            pass

    suggested = int(os.getenv("AI_APPROVE_MIN_CONFIDENCE", "68"))
    status = "default"

    if trainer.model is not None:
        import pandas as pd

        for col in trainer.feature_columns:
            if col not in df.columns:
                df[col] = 0
        X = df[trainer.feature_columns].fillna(0)
        probs = trainer.model.predict(X)
        df = df.copy()
        df["ml_prob"] = probs

        best_floor = suggested
        best_prec = 0.0
        for floor_pct in range(55, 96, 2):
            mask = df["ml_prob"] >= floor_pct / 100.0
            if mask.sum() < 10:
                continue
            prec = float(df.loc[mask, "was_profitable"].mean())
            if prec >= 0.55 and prec > best_prec:
                best_prec = prec
                best_floor = floor_pct
        suggested = best_floor
        status = "calibrated"
        payload = {
            "suggested_ai_approve_min_confidence": suggested,
            "precision_at_floor": round(best_prec, 3),
            "n_samples": len(df),
            "status": status,
        }
    else:
        payload = {
            "suggested_ai_approve_min_confidence": suggested,
            "n_samples": len(df),
            "status": "model_missing",
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "ML calibration | suggested AI_APPROVE_MIN_CONFIDENCE=%s (%s)",
        suggested,
        status,
    )
    return payload
