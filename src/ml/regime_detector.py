"""
Market regime classifier (quiet / spike / mean_reverting) for ML ensemble weighting.

Train: ``python -m src.ml.regime_detector`` or ``npm run train:ml:regime``
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REGIMES = ("quiet", "spike", "mean_reverting")
DEFAULT_MODEL = Path(os.getenv("ML_REGIME_MODEL_PATH", "models/regime_detector.joblib"))
DEFAULT_META = Path(os.getenv("ML_REGIME_META_PATH", "models/regime_detector_meta.json"))
NEAR_MISS_LOG = Path("logs/cex_dex_near_misses.jsonl")


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def build_features(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Features: gross_bps, net_bps, vol proxy from |gross|, hour."""
    records = []
    for r in rows:
        gross = float(r.get("gross_bps") or 0)
        net = float(r.get("net_bps") or 0)
        ts = r.get("timestamp") or r.get("ts")
        hour = 12
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    hour = datetime.fromtimestamp(float(ts), tz=UTC).hour
                else:
                    hour = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour
            except Exception:
                pass
        records.append(
            {
                "gross_bps": gross,
                "net_bps": net,
                "abs_gross": abs(gross),
                "hour_utc": hour,
            }
        )
    return pd.DataFrame(records)


def label_regime(row: pd.Series) -> str:
    g = float(row["gross_bps"])
    ag = float(row["abs_gross"])
    if ag >= 15:
        return "spike"
    if ag <= 3 and abs(g) <= 2:
        return "quiet"
    return "mean_reverting"


def train_from_logs(
    *,
    near_miss_path: Path = NEAR_MISS_LOG,
    min_samples: int = 50,
) -> dict[str, Any]:
    rows = _read_jsonl(near_miss_path, limit=20_000)
    df = build_features(rows)
    if len(df) < min_samples:
        return {"status": "insufficient_data", "samples": len(df), "min_samples": min_samples}

    df["regime"] = df.apply(label_regime, axis=1)
    feature_cols = ["gross_bps", "net_bps", "abs_gross", "hour_utc"]
    X = df[feature_cols].values
    y = df["regime"].values

    from lightgbm import LGBMClassifier

    clf = LGBMClassifier(
        n_estimators=80,
        max_depth=4,
        learning_rate=0.08,
        random_state=42,
    )
    clf.fit(X, y)

    DEFAULT_MODEL.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": clf, "features": feature_cols, "regimes": list(REGIMES)}, DEFAULT_MODEL)

    meta = {
        "trained_utc": datetime.now(UTC).isoformat(),
        "samples": len(df),
        "class_counts": df["regime"].value_counts().to_dict(),
        "features": feature_cols,
    }
    DEFAULT_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("Regime model saved | %s samples → %s", len(df), DEFAULT_MODEL)
    return {"status": "ok", **meta}


def predict_regime(
    gross_bps: float,
    net_bps: float,
    *,
    hour_utc: int | None = None,
) -> tuple[str, dict[str, float]]:
    """Return regime label and class probabilities."""
    if not DEFAULT_MODEL.is_file():
        ag = abs(gross_bps)
        if ag >= 15:
            return "spike", {"spike": 1.0}
        if ag <= 3:
            return "quiet", {"quiet": 1.0}
        return "mean_reverting", {"mean_reverting": 1.0}

    bundle = joblib.load(DEFAULT_MODEL)
    clf = bundle["model"]
    cols = bundle["features"]
    hour = hour_utc if hour_utc is not None else datetime.now(UTC).hour
    row = pd.DataFrame(
        [
            {
                "gross_bps": gross_bps,
                "net_bps": net_bps,
                "abs_gross": abs(gross_bps),
                "hour_utc": hour,
            }
        ]
    )
    X = row[cols].values
    pred = str(clf.predict(X)[0])
    probs_arr = clf.predict_proba(X)[0]
    classes = list(clf.classes_)
    probs = {str(c): float(p) for c, p in zip(classes, probs_arr, strict=False)}
    return pred, probs


def ensemble_weights_for_regime(regime: str) -> dict[str, float]:
    """Heuristic-heavy in quiet regimes (Plan: Phase 4 ML)."""
    quiet_heur = float(os.getenv("ML_REGIME_QUIET_HEURISTIC_WEIGHT", "0.55"))
    quiet_lgbm = float(os.getenv("ML_REGIME_QUIET_LGBM_WEIGHT", "0.30"))
    quiet_reg = float(os.getenv("ML_REGIME_QUIET_REGIME_WEIGHT", "0.15"))
    default_heur = float(os.getenv("ML_ENSEMBLE_HEURISTIC_WEIGHT", "0.35"))
    default_lgbm = float(os.getenv("ML_ENSEMBLE_LGBM_WEIGHT", "0.50"))
    default_reg = float(os.getenv("ML_ENSEMBLE_REGIME_WEIGHT", "0.15"))

    if regime == "quiet":
        return {
            "heuristic": quiet_heur,
            "lgbm": quiet_lgbm,
            "regime": quiet_reg,
        }
    return {
        "heuristic": default_heur,
        "lgbm": default_lgbm,
        "regime": default_reg,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    result = train_from_logs()
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
