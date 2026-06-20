#!/usr/bin/env python3
"""
Zero-cost gate auto-tuner: v2 attempt telemetry + fill drift analysis.

Reads ``logs/v2_attempts.jsonl`` for market regime (hot/warm/tight) and suggests
safe v2 gate tweaks. Optionally compares modeled vs realized net from fills.

Writes ``logs/auto_tune_suggestions.json``. Apply with ``--apply`` (safe keys only).
Run on a schedule: ``npm run tune:auto:watch`` (every 15 min).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
V2_ATTEMPTS_LOG = Path(
    os.getenv("V2_ATTEMPTS_LOG", "logs/v2_attempts.jsonl").strip()
    or "logs/v2_attempts.jsonl"
)
TRADE_LOG = Path(os.getenv("TRADE_HISTORY_PATH", "logs/trade_history.jsonl"))
V2_PNL_LOG = Path(os.getenv("V2_PNL_LOG", "logs/v2_pnl.jsonl"))
OUT_PATH = Path(os.getenv("AUTO_TUNE_OUTPUT", "logs/auto_tune_suggestions.json"))
ENV_PATH = Path(os.getenv("AUTO_TUNE_ENV_FILE", ".env"))
RESCUE_LOG = Path(os.getenv("AUTO_TUNE_RESCUE_LOG", "logs/v2.log"))
RESCUE_POLICY_PATH = Path(
    os.getenv("AUTO_TUNE_RESCUE_POLICY_OUTPUT", "logs/rescue_pair_policy.json")
)

HOT_GROSS_BPS = float(os.getenv("AUTO_TUNE_HOT_GROSS_BPS", "15"))
WARM_GROSS_BPS = float(os.getenv("AUTO_TUNE_WARM_GROSS_BPS", "8"))
WINDOW_ROWS = int(os.getenv("AUTO_TUNE_WINDOW_ROWS", "500"))
RESCUE_WINDOW_LINES = int(os.getenv("AUTO_TUNE_RESCUE_WINDOW_LINES", "5000"))

SAFE_KEYS = frozenset(
    {
        "CEX_DEX_STRATEGY_BASE_COST_BPS",
        "CEX_DEX_MIN_GROSS_SPREAD_BPS",
        "CEX_DEX_MIN_NET_SPREAD_BPS",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS",
        "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE",
        "CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR",
        "AI_APPROVE_MIN_CONFIDENCE",
        "JITO_TIP_FILL_RATE_TARGET",
        "V2_MIN_NET_BPS",
        "V2_MIN_NET_BPS_BASE",
        "V2_MIN_GROSS_BPS",
        "V2_MIN_GROSS_BPS_BASE",
        "V2_DETECT_MIN_GROSS_FLOOR",
    }
)

FILL_MODE_OVERRIDES: dict[str, str] = {
    "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS": "0.18",
    "CEX_DEX_ROUNDTRIP_SIM_MIN_NET_BPS_GO_LIVE": "0.16",
    "CEX_DEX_MIN_NET_SPREAD_BPS": "1.2",
    "CEX_DEX_ROUNDTRIP_SOFT_PASS_FACTOR": "0.72",
    "AI_APPROVE_MIN_CONFIDENCE": "50",
    "JITO_TIP_FILL_RATE_TARGET": "0.40",
    "V2_MIN_NET_BPS": "1.0",
    "V2_MIN_NET_BPS_BASE": "1.0",
}


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    q = max(0.0, min(1.0, q))
    arr = sorted(float(v) for v in values)
    idx = int((len(arr) - 1) * q)
    return arr[idx]


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.is_file() or limit <= 0:
        return []
    dq: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            dq.append(line.rstrip("\n"))
    return list(dq)


def analyze_rescue_policy(*, line_window: int = RESCUE_WINDOW_LINES) -> dict[str, Any]:
    """Build pair-level negative-sim policy from rescue pass/block telemetry."""
    lines = _tail_lines(RESCUE_LOG, line_window)
    if not lines:
        return {"status": "no_rescue_logs", "pairs": {}, "suggestions": []}

    pair_stats: dict[str, dict[str, Any]] = {}
    kv_re = re.compile(r"(\w+)=([^\s]+)")

    def _pair_state(name: str) -> dict[str, Any]:
        sym = (name.split("/")[0] if "/" in name else name).strip().upper()
        if sym not in pair_stats:
            pair_stats[sym] = {
                "sim_nets": [],
                "edges": [],
                "confs": [],
                "sizes": [],
                "pass": 0,
                "blocked": 0,
                "blocked_by": {},
            }
        return pair_stats[sym]

    for line in lines:
        if (
            "MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_PASS" not in line
            and "MODEL_NET_SOFT_RESCUE_NEGATIVE_SIM_BLOCKED" not in line
            and "MODEL_NET_SOFT_RESCUE_SKIP" not in line
        ):
            continue
        row = {m.group(1): m.group(2) for m in kv_re.finditer(line)}
        pair = str(row.get("pair") or "").strip()
        if not pair:
            continue
        st = _pair_state(pair)

        sim_raw = row.get("sim_net") or row.get("best_sim_net")
        if sim_raw and sim_raw not in ("n/a", "inf", "-inf"):
            try:
                sim = float(str(sim_raw).replace("bps", ""))
                st["sim_nets"].append(sim)
            except (TypeError, ValueError):
                pass
        edge_raw = row.get("edge")
        if edge_raw:
            try:
                st["edges"].append(float(edge_raw))
            except (TypeError, ValueError):
                pass
        conf_raw = row.get("conf")
        if conf_raw:
            try:
                st["confs"].append(float(conf_raw))
            except (TypeError, ValueError):
                pass
        size_raw = row.get("size_usdc") or row.get("best_size_usdc")
        if size_raw:
            try:
                st["sizes"].append(float(size_raw))
            except (TypeError, ValueError):
                pass

        if "NEGATIVE_SIM_PASS" in line:
            st["pass"] += 1
        if "NEGATIVE_SIM_BLOCKED" in line:
            st["blocked"] += 1
            blocked_by = str(row.get("blocked_by") or "unknown")
            for reason in blocked_by.split(","):
                key = reason.strip().lower() or "unknown"
                st["blocked_by"][key] = int(st["blocked_by"].get(key, 0)) + 1

    pairs_policy: dict[str, Any] = {}
    suggestions: list[dict[str, Any]] = []
    for sym, st in pair_stats.items():
        if len(st["sim_nets"]) < 4:
            continue
        sims = [float(v) for v in st["sim_nets"] if float(v) < 0]
        if not sims:
            continue
        abs_losses = [abs(v) for v in sims]
        max_loss = max(3.0, min(12.0, _percentile(abs_losses, 0.8) + 0.5))
        min_edge = max(20.0, min(70.0, _percentile(st["edges"] or [35.0], 0.35) - 0.5))
        min_conf = max(55.0, min(95.0, _percentile(st["confs"] or [75.0], 0.35) - 0.5))
        max_size = max(3.0, min(20.0, _percentile(st["sizes"] or [8.0], 0.7)))
        pairs_policy[sym] = {
            "enabled": True,
            "max_loss_bps": round(max_loss, 2),
            "min_edge_bps": round(min_edge, 2),
            "min_ai_conf": round(min_conf, 2),
            "max_size_usdc": round(max_size, 2),
            "sim_timeout_retries": 1 if st["blocked_by"].get("edge", 0) else 0,
            "sample_count": len(st["sim_nets"]),
            "blocked_by": st["blocked_by"],
            "pass_count": int(st["pass"]),
        }
        suggestions.append(
            {
                "pair": sym,
                "reason": "pair_rescue_policy_refresh",
                "max_loss_bps": round(max_loss, 2),
                "min_edge_bps": round(min_edge, 2),
                "min_ai_conf": round(min_conf, 2),
                "max_size_usdc": round(max_size, 2),
            }
        )

    if not pairs_policy:
        return {"status": "insufficient_samples", "pairs": {}, "suggestions": []}

    out = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source_log": str(RESCUE_LOG),
        "pairs": pairs_policy,
    }
    RESCUE_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESCUE_POLICY_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "pair_count": len(pairs_policy),
        "output_path": str(RESCUE_POLICY_PATH),
        "pairs": pairs_policy,
        "suggestions": suggestions,
    }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _gross_bps(row: dict[str, Any]) -> float | None:
    for key in ("gross_bps", "gross", "scan_gross_bps"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _net_bps(row: dict[str, Any]) -> float | None:
    for key in ("net_bps", "net", "roundtrip_net_bps"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def auto_tune_gates(
    log_file: Path | str = V2_ATTEMPTS_LOG,
    *,
    window: int = WINDOW_ROWS,
) -> dict[str, Any]:
    """Analyze recent v2 cycles and suggest gate adjustments by market regime."""
    path = Path(log_file)
    data = _read_jsonl(path)
    recent = [d for d in data[-window:] if _gross_bps(d) is not None]

    if not recent:
        return {
            "status": "no_attempts",
            "regime": "unknown",
            "attempt_count": 0,
            "suggestions": [],
            "message": "No v2 attempt rows with gross — waiting for scan data",
        }

    gross_vals = [float(_gross_bps(d)) for d in recent if _gross_bps(d) is not None]
    net_vals = [float(_net_bps(d)) for d in recent if _net_bps(d) is not None]
    avg_gross = mean(gross_vals) if gross_vals else 0.0
    avg_net = mean(net_vals) if net_vals else 0.0
    max_gross = max(gross_vals) if gross_vals else 0.0

    dex_cheap = [d for d in recent if str(d.get("spread_direction") or "") == "dex_cheap"]
    dex_cheap_gross = [
        float(_gross_bps(d)) for d in dex_cheap if _gross_bps(d) is not None
    ]
    avg_dex_cheap_gross = mean(dex_cheap_gross) if dex_cheap_gross else avg_gross

    roundtrip_blocks = sum(
        1
        for d in recent
        if str(d.get("block_reason") or "").startswith("roundtrip")
    )
    near_miss = sum(
        1
        for d in recent
        if (_gross_bps(d) or 0) >= WARM_GROSS_BPS
        and str(d.get("block_reason") or "").startswith("roundtrip")
    )
    strong_signals = sum(
        1 for d in recent if str(d.get("block_reason") or "") == "STRONG_DEX_CHEAP_SIGNAL"
    )
    fills = sum(1 for d in recent if d.get("live_fill") is True)

    current_net_base = _env_float("V2_MIN_NET_BPS_BASE", 0.3)
    current_net = _env_float("V2_MIN_NET_BPS", 0.5)
    current_gross_floor = _env_float("V2_DETECT_MIN_GROSS_FLOOR", 1.4)

    suggestions: list[dict[str, Any]] = []
    message = ""

    if avg_gross > HOT_GROSS_BPS:
        regime = "hot"
        new_net_base = max(0.15, min(current_net_base, 0.25))
        new_net = max(0.2, min(current_net, 0.3))
        message = f"Market hot (avg gross {avg_gross:.1f} bps) → loosen net floor to {new_net:g}"
        if new_net_base < current_net_base - 0.02:
            suggestions.append(
                {
                    "key": "V2_MIN_NET_BPS_BASE",
                    "current": str(current_net_base),
                    "suggested": f"{new_net_base:g}",
                    "reason": f"hot market avg_gross={avg_gross:.1f} max={max_gross:.1f}",
                }
            )
        if new_net < current_net - 0.02:
            suggestions.append(
                {
                    "key": "V2_MIN_NET_BPS",
                    "current": str(current_net),
                    "suggested": f"{new_net:g}",
                    "reason": f"hot market — capture more edges (near_miss={near_miss})",
                }
            )
    elif avg_gross > WARM_GROSS_BPS:
        regime = "warm"
        message = (
            f"Warm market (avg gross {avg_gross:.1f} bps, max {max_gross:.1f}) → "
            f"hold current gates (net_base={current_net_base:g})"
        )
        if near_miss >= 5 and roundtrip_blocks > near_miss * 0.6:
            message += " — roundtrip sim blocking; do not loosen net"
    else:
        regime = "tight"
        message = (
            f"Tight market (avg gross {avg_gross:.1f} bps) → keep strict gates "
            f"(net_base={current_net_base:g})"
        )
        if avg_dex_cheap_gross < WARM_GROSS_BPS * 0.5 and current_net_base < 0.3:
            new_net_base = 0.3
            suggestions.append(
                {
                    "key": "V2_MIN_NET_BPS_BASE",
                    "current": str(current_net_base),
                    "suggested": f"{new_net_base:g}",
                    "reason": f"tight market dex_cheap avg={avg_dex_cheap_gross:.1f} bps",
                }
            )
        if current_gross_floor < 1.4 and avg_gross < 3:
            suggestions.append(
                {
                    "key": "V2_DETECT_MIN_GROSS_FLOOR",
                    "current": str(current_gross_floor),
                    "suggested": "1.4",
                    "reason": "tight market — restore default gross floor",
                }
            )

    return {
        "status": "ok",
        "regime": regime,
        "attempt_count": len(recent),
        "avg_gross_bps": round(avg_gross, 3),
        "avg_net_bps": round(avg_net, 3),
        "max_gross_bps": round(max_gross, 3),
        "avg_dex_cheap_gross_bps": round(avg_dex_cheap_gross, 3),
        "roundtrip_blocks": roundtrip_blocks,
        "near_miss_roundtrip": near_miss,
        "strong_signal_count": strong_signals,
        "live_fills_in_window": fills,
        "message": message,
        "suggestions": suggestions,
    }


def _fill_realized_bps(row: dict[str, Any]) -> float | None:
    """Best-effort realized net bps (prefer net_usdc / trade_usdc over trade_history proceeds)."""
    trade = float(row.get("trade_usdc") or row.get("size_usdc") or 0)
    net_usdc = row.get("net_usdc")
    if net_usdc is not None and trade > 0:
        return (float(net_usdc) / trade) * 10_000.0
    if row.get("net_bps") is not None and str(row.get("source") or "") in (
        "live",
        "live_fill",
        "backfill",
    ):
        return float(row["net_bps"])
    realized = row.get("realized_usdc")
    if realized is not None and trade > 0:
        bps = (float(realized) / trade) * 10_000.0
        if 0 <= bps <= 200:
            return bps
    return None


def analyze_fill_drift() -> dict[str, Any]:
    """Compare modeled vs realized net from live fills (v2_pnl + trade_history)."""
    seen_tx: set[str] = set()
    fills: list[dict[str, Any]] = []
    for path in (V2_PNL_LOG, TRADE_LOG):
        for r in _read_jsonl(path):
            if not (
                r.get("live_fill") is True
                or str(r.get("source") or "") in ("live", "live_fill", "backfill")
            ):
                continue
            tx = str(r.get("tx_sig") or "")
            if tx and tx in seen_tx:
                continue
            if tx:
                seen_tx.add(tx)
            fills.append(r)
    if not fills:
        return {"status": "no_fills", "suggestions": []}

    modeled_nets = [float(r.get("net_bps") or 0) for r in fills]
    realized_bps = [b for r in fills if (b := _fill_realized_bps(r)) is not None]
    if not realized_bps:
        return {"status": "no_fills", "suggestions": []}

    avg_modeled = mean(modeled_nets) if modeled_nets else 0.0
    avg_realized_bps = mean(realized_bps) if realized_bps else 0.0
    drift = avg_modeled - avg_realized_bps

    suggestions: list[dict[str, Any]] = []
    current_cost = int(os.getenv("CEX_DEX_STRATEGY_BASE_COST_BPS", "14"))
    if abs(drift) >= 2.0:
        delta = -2 if drift > 2 else 2
        new_cost = max(8, min(25, current_cost + delta))
        suggestions.append(
            {
                "key": "CEX_DEX_STRATEGY_BASE_COST_BPS",
                "current": str(current_cost),
                "suggested": str(new_cost),
                "reason": f"modeled-realized net drift {drift:.1f} bps over {len(fills)} fills",
            }
        )

    if avg_realized_bps < 0 and len(fills) >= 3:
        gross_floor = int(os.getenv("CEX_DEX_MIN_GROSS_SPREAD_BPS", "12"))
        suggestions.append(
            {
                "key": "CEX_DEX_MIN_GROSS_SPREAD_BPS",
                "current": str(gross_floor),
                "suggested": str(min(20, gross_floor + 2)),
                "reason": "negative realized bps on recent fills — tighten gross",
            }
        )

    return {
        "status": "ok",
        "fill_count": len(fills),
        "avg_modeled_net_bps": round(avg_modeled, 2),
        "avg_realized_bps": round(avg_realized_bps, 2),
        "drift_bps": round(drift, 2),
        "suggestions": suggestions,
    }


def analyze(
    *,
    log_file: Path | str = V2_ATTEMPTS_LOG,
    window: int = WINDOW_ROWS,
) -> dict[str, Any]:
    """Full report: v2 gate regime + fill drift."""
    gate_report = auto_tune_gates(log_file, window=window)
    fill_report = analyze_fill_drift()
    rescue_policy_report = analyze_rescue_policy()

    all_suggestions = list(gate_report.get("suggestions") or [])
    for sug in fill_report.get("suggestions") or []:
        if not any(s.get("key") == sug.get("key") for s in all_suggestions):
            all_suggestions.append(sug)
    for sug in rescue_policy_report.get("suggestions") or []:
        all_suggestions.append(sug)

    return {
        "generated_utc": datetime.now(UTC).isoformat(),
        "gate_analysis": gate_report,
        "fill_analysis": fill_report,
        "rescue_policy_analysis": rescue_policy_report,
        "suggestions": all_suggestions,
        "message": gate_report.get("message", ""),
    }


def tune_for_fills(*, apply: bool = False) -> dict[str, Any]:
    """Aggressive gate overrides to break through sim blocks (first live_fill push)."""
    suggestions = [
        {
            "key": key,
            "current": os.getenv(key, ""),
            "suggested": val,
            "reason": "fill_mode_aggressive",
        }
        for key, val in FILL_MODE_OVERRIDES.items()
    ]
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "mode": "fill_aggressive",
        "message": "Aggressive fill-mode gate overrides",
        "suggestions": suggestions,
    }
    if apply:
        _apply_suggestions(report)
    return report


def _apply_suggestions(report: dict[str, Any]) -> int:
    if not ENV_PATH.is_file():
        print(f"Missing {ENV_PATH}")
        return 1
    content = ENV_PATH.read_text(encoding="utf-8")
    applied = 0
    for sug in report.get("suggestions") or []:
        key = str(sug.get("key") or "")
        val = str(sug.get("suggested") or "")
        if key not in SAFE_KEYS:
            continue
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(f"{key}={val}", content)
        else:
            content += f"\n{key}={val}\n"
        applied += 1
        print(f"Applied {key}={val}")
    ENV_PATH.write_text(content, encoding="utf-8")
    print(f"Applied {applied} keys to {ENV_PATH}")
    return 0


def _print_summary(report: dict[str, Any]) -> None:
    gate = report.get("gate_analysis") or {}
    msg = gate.get("message") or report.get("message") or ""
    if msg:
        print(msg)
    regime = gate.get("regime", "unknown")
    avg = gate.get("avg_gross_bps")
    attempts = gate.get("attempt_count", 0)
    if attempts:
        print(
            f"Regime={regime} | attempts={attempts} | avg_gross={avg} bps | "
            f"roundtrip_blocks={gate.get('roundtrip_blocks', 0)} | "
            f"fills={gate.get('live_fills_in_window', 0)}"
        )
    suggestions = report.get("suggestions") or []
    if suggestions:
        print(f"{len(suggestions)} suggestion(s):")
        for sug in suggestions:
            if sug.get("key"):
                print(
                    f"  {sug.get('key')}: {sug.get('current')} → {sug.get('suggested')} "
                    f"({sug.get('reason')})"
                )
            else:
                print(f"  {sug}")
    else:
        print("No gate changes suggested.")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2 gate auto-tuner (zero-cost monitoring)")
    parser.add_argument(
        "--log",
        default=str(V2_ATTEMPTS_LOG),
        help="v2 attempts jsonl path",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=WINDOW_ROWS,
        help="Recent rows to analyze",
    )
    parser.add_argument("--apply", action="store_true", help="Write safe keys to .env")
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SEC",
        default=0,
        help="Re-run every SEC seconds (e.g. 900 = 15 min)",
    )
    parser.add_argument(
        "--fill-mode",
        action="store_true",
        help="Apply aggressive fill-rate gate overrides (first live_fill push)",
    )
    args = parser.parse_args()

    if args.fill_mode:
        report = tune_for_fills(apply=args.apply)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _print_summary(report)
        if not args.apply:
            print(json.dumps(report, indent=2))
        return 0

    def _run_once() -> dict[str, Any]:
        report = analyze(log_file=args.log, window=args.window)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _print_summary(report)
        if args.apply and report.get("suggestions"):
            _apply_suggestions(report)
        return report

    if args.watch > 0:
        print(f"Auto-tuner watch mode: every {args.watch}s → {OUT_PATH}")
        while True:
            try:
                print(f"\n--- {datetime.now(UTC).isoformat()} ---")
                _run_once()
            except KeyboardInterrupt:
                print("Stopped.")
                return 0
            except Exception as exc:
                print(f"Auto-tuner error: {exc}", file=sys.stderr)
            time.sleep(max(60, int(args.watch)))

    report = _run_once()
    if not args.apply:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
