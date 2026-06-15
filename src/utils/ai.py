"""
src/utils/ai.py
OpenAI client with retry, logging, and structured output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings, settings
from src.strategies.brain_signals import (
    apply_weak_cex_dex_score_bias,
    cex_dex_gross_bps_from_snapshot,
    preferred_lane_when_weak_cex_dex,
)
from src.utils.ai_prompts import (
    AIPrompts,
    dumps_json,
    get_ai_decision_prompt,
    prompt_version,
    render_prompt,
)

logger = logging.getLogger(__name__)

_openai_client: AsyncOpenAI | None = None

_AI_FAIL_CLOSED = {
    "approve": False,
    "confidence": 0,
    "reason": "AI call failed - fail closed",
}

# Cycle brain: ordering + optional bias. ``cex_dex`` gets extra bias when snapshot shows it active.
_STRATEGY_KEYS = (
    "liquidation",
    "collateral_swap",
    "backrun",
    "cex_dex",
    "dex_cex_reverse",
)
_DEFAULT_PRIORITY_ORDER = (
    "cex_dex",
    "dex_cex_reverse",
    "backrun",
    "collateral_swap",
    "liquidation",
)
_PRIORITY_BIAS_KEYS = frozenset({"collateral_swap", "backrun", "dex_cex_reverse"})


def _parse_strategy_priority_order() -> list[str]:
    raw = (os.getenv("STRATEGY_PRIORITY_ORDER") or "").strip()
    if not raw:
        return list(_DEFAULT_PRIORITY_ORDER)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    for k in _STRATEGY_KEYS:
        if k not in seen:
            out.append(k)
    return out


def _strategy_win_threshold() -> float:
    try:
        return float(os.getenv("STRATEGY_WIN_THRESHOLD", "28"))
    except (TypeError, ValueError):
        return 28.0


def _strategy_priority_score_bias() -> float:
    try:
        return float(os.getenv("STRATEGY_PRIORITY_SCORE_BIAS", "14"))
    except (TypeError, ValueError):
        return 14.0


def _cex_dex_active(snapshot: dict | None) -> bool:
    cx = (snapshot or {}).get("cex_dex") or {}
    return bool(cx.get("active"))


def _cex_dex_brain_priority_bias() -> float:
    """Extra score bias for ``cex_dex`` when ``snapshot["cex_dex"].active`` (wins ties vs other biased lanes)."""
    try:
        return float(os.getenv("CEX_DEX_BRAIN_PRIORITY_BIAS", "48"))
    except (TypeError, ValueError):
        return 48.0


def pick_best_strategy_with_priority(
    scores: dict,
    snapshot: dict | None = None,
) -> tuple[str, dict[str, float]]:
    """
    Choose best_strategy using raw scores, optional bias on collateral/backrun,
    extra bias on cex_dex when that lane is active in the snapshot, tie-break order.

    Returns (best_strategy, adjusted_scores used for selection).
    """
    priority_order = _parse_strategy_priority_order()
    prio_rank = {name: i for i, name in enumerate(priority_order)}
    bias_amt = _strategy_priority_score_bias()
    from src.strategies.brain import cex_dex_dynamic_bias_from_snapshot

    weak_lane = preferred_lane_when_weak_cex_dex(snapshot)
    threshold = _strategy_win_threshold()

    from src.strategies.brain_signals import lane_signal_present

    adjusted: dict[str, float] = {}
    for k in _STRATEGY_KEYS:
        try:
            v = float(scores.get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if k in _PRIORITY_BIAS_KEYS and lane_signal_present(snapshot, k):
            v += bias_amt
        if k == "cex_dex":
            dynamic = cex_dex_dynamic_bias_from_snapshot(snapshot)
            if dynamic > 0:
                v += dynamic
            elif _cex_dex_active(snapshot) and weak_lane is None:
                v += _cex_dex_brain_priority_bias()
        adjusted[k] = v

    forced_lane, adjusted = apply_weak_cex_dex_score_bias(adjusted, snapshot)
    if forced_lane is not None:
        return forced_lane, adjusted

    best_key = max(
        _STRATEGY_KEYS,
        key=lambda k: (adjusted[k], -prio_rank.get(k, 999)),
    )
    if adjusted[best_key] < threshold:
        return "none", adjusted
    return best_key, adjusted


def _openai_api_key() -> str:
    cfg = get_settings()
    key = (
        getattr(cfg, "openai_api_key", None)
        or getattr(cfg, "OPENAI_API_KEY", None)
        or os.getenv("OPENAI_API_KEY")
        or ""
    )
    return str(key).strip()


def _get_openai_client() -> AsyncOpenAI | None:
    global _openai_client
    key = _openai_api_key()
    if not key or key.lower() in ("changeme", "placeholder", "your_key_here"):
        return None
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client


def _openai_model() -> str:
    return (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _chat_json_decision(
    *,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 300,
) -> dict[str, Any]:
    client = _get_openai_client()
    if client is None:
        raise RuntimeError("OPENAI_API_KEY missing or placeholder")

    response = await client.chat.completions.create(
        model=_openai_model(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content or "").strip()
    parsed = _parse_json_dict(content)
    if parsed is None:
        raise ValueError("AI JSON parse failed")
    return parsed


async def get_ai_decision(opp: dict[str, Any], strategy: str = "cex_dex") -> dict[str, Any]:
    """Get AI trading decision with structured JSON output (fail-closed on error)."""
    try:
        prompts = get_ai_decision_prompt(strategy, opp)
        decision = await _chat_json_decision(
            system=prompts["system"],
            user=prompts["user"],
        )

        approve = decision.get("approve")
        if approve is None:
            action = str(decision.get("decision") or "").strip().upper()
            approve = action == "APPROVE"
        else:
            approve = bool(approve)

        try:
            confidence = int(decision.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0

        reason = str(decision.get("reason") or decision.get("reasoning") or "")
        logger.info(
            "AI Decision [%s]: approve=%s confidence=%s",
            strategy,
            approve,
            confidence,
        )
        return {
            "approve": approve,
            "confidence": confidence,
            "reason": reason,
            "risk_factors": decision.get("risk_factors"),
            "suggested_size_usdc": decision.get("suggested_size_usdc"),
            "source": "openai",
        }
    except Exception as exc:
        logger.error("AI decision failed: %s", exc)
        return {**_AI_FAIL_CLOSED, "source": "fail_closed"}


async def daily_strategy_review(stats: dict[str, Any]) -> str | None:
    """Daily AI performance review (returns model text or None if unavailable)."""
    client = _get_openai_client()
    if client is None:
        logger.warning("daily_strategy_review: OpenAI client unavailable")
        return None

    prompts = AIPrompts()
    try:
        response = await client.chat.completions.create(
            model=_openai_model(),
            messages=[
                {"role": "system", "content": prompts.get_system_prompt()},
                {
                    "role": "user",
                    "content": prompts.get_daily_strategy_review_prompt(stats),
                },
            ],
            temperature=0.55,
            max_tokens=1100,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text or text.startswith("Error calling OpenAI"):
            return None
        return text
    except Exception as exc:
        logger.warning("daily_strategy_review failed: %s", exc)
        return None


def _parse_json_dict(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            out = json.loads(raw[start : end + 1])
            return out if isinstance(out, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def ask_openai(prompt: str, temperature: float = 0.7, max_tokens: int = 800) -> str:
    """Core helper to call OpenAI with short retries."""
    logger.debug("OpenAI call | prompt_version=%s", prompt_version())
    client = _get_openai_client()
    if client is None:
        logger.warning("OPENAI_API_KEY missing or placeholder; skipping model call")
        return "Error calling OpenAI"
    last_err: BaseException | None = None
    for attempt in range(3):
        try:
            model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except Exception as e:
            last_err = e
            logger.warning(
                "OpenAI attempt %s/3 failed (%s): %s",
                attempt + 1,
                type(e).__name__,
                e,
            )
            await asyncio.sleep(1.0 + float(attempt))
    logger.warning("OpenAI exhausted retries (%s)", type(last_err).__name__ if last_err else "?")
    return "Error calling OpenAI"


def _min_net_profit_bps() -> int:
    try:
        return int(
            os.getenv(
                "MIN_NET_PROFIT_BPS",
                os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", str(settings.MIN_NET_PROFIT_BPS)),
            )
        )
    except (TypeError, ValueError):
        return int(settings.MIN_NET_PROFIT_BPS)


def _ai_approve_min_confidence_floor(min_confidence: int | None = None) -> int:
    if min_confidence is not None:
        try:
            base = int(min_confidence)
        except (TypeError, ValueError):
            base = int(settings.AI_APPROVE_MIN_CONFIDENCE)
    else:
        try:
            base = int(
                os.getenv("AI_APPROVE_MIN_CONFIDENCE", str(settings.AI_APPROVE_MIN_CONFIDENCE))
            )
        except (TypeError, ValueError):
            base = int(settings.AI_APPROVE_MIN_CONFIDENCE)
    try:
        from src.strategies.brain_pnl import bump_min_confidence_for_recent_pnl

        base = bump_min_confidence_for_recent_pnl(base)
    except Exception:
        pass
    return max(0, min(100, base))


def _max_volatility_bps_for_ai() -> float:
    try:
        return float(os.getenv("AI_MAX_VOLATILITY_BPS", "80"))
    except (TypeError, ValueError):
        return 80.0


def _enrich_signal_for_enhanced_ai(signal: dict[str, Any]) -> dict[str, Any]:
    """Attach brain snapshot, rolling PnL, and threshold hints for the conservative gate."""
    out = dict(signal)
    try:
        from src.strategies.brain_pnl import rolling_pnl_sum_usd
        from src.strategies.brain_signals import brain_snapshot

        snapshot = brain_snapshot()
        out.setdefault("brain_snapshot", snapshot)
        cex_ctx = snapshot.get("cex_dex") or snapshot.get("cex_dex_best") or {}
        if cex_ctx and "volatility_bps" not in out:
            out["volatility_bps"] = cex_ctx.get("volatility_bps")

        hours = float(os.getenv("AI_PNL_CONFIDENCE_WINDOW_HOURS", "72"))
        window_sec = max(3600.0, hours * 3600.0)
        out["rolling_pnl_usd"] = rolling_pnl_sum_usd(window_seconds=window_sec)
        out["pnl_window_hours"] = hours
    except Exception as exc:
        logger.debug("enhanced_ai context enrich skipped: %s", exc)

    out.setdefault("min_net_profit_bps", _min_net_profit_bps())
    inv_max = os.getenv("CEX_DEX_MAX_INVENTORY_SOL") or os.getenv("INVENTORY_MAX_SOL")
    if inv_max:
        out.setdefault("max_inventory_sol", float(inv_max))
    return out


def _heuristic_enhanced_approve(signal: dict[str, Any], min_conf: int) -> dict[str, Any]:
    """Conservative fallback when OpenAI is unavailable."""
    min_bps = int(signal.get("min_net_profit_bps") or _min_net_profit_bps())
    gross = signal.get("gross_bps")
    net = signal.get("spread_bps_net")
    edge_bps = None
    for candidate in (gross, net):
        if candidate is not None:
            try:
                edge_bps = float(candidate)
                break
            except (TypeError, ValueError):
                continue

    vol = 0.0
    try:
        vol = float(signal.get("volatility_bps") or 0.0)
    except (TypeError, ValueError):
        vol = 0.0

    pnl = 0.0
    try:
        pnl = float(signal.get("rolling_pnl_usd") or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0

    reasons: list[str] = []
    approve = True

    if edge_bps is None or edge_bps < min_bps:
        approve = False
        reasons.append(f"edge_bps_below_{min_bps}")
    if vol > _max_volatility_bps_for_ai():
        approve = False
        reasons.append("volatility_high")
    if pnl < float(os.getenv("AI_PNL_CONFIDENCE_NEUTRAL_USD", "0")):
        approve = False
        reasons.append("pnl_window_negative")

    inv_sol = signal.get("inventory_sol")
    inv_max = signal.get("max_inventory_sol")
    if inv_sol is not None and inv_max is not None:
        try:
            if float(inv_sol) > float(inv_max) * 0.92:
                approve = False
                reasons.append("inventory_skewed")
        except (TypeError, ValueError):
            pass

    confidence = 72 if approve else 25
    return {
        "approve": approve,
        "confidence": confidence,
        "reason": "; ".join(reasons) if reasons else "heuristic_ok",
        "source": "heuristic",
    }


def _parse_enhanced_ai_response(raw: str) -> dict[str, Any] | None:
    data = _parse_json_dict(raw)
    if not data:
        return None
    approve = data.get("approve")
    if approve is None:
        decision = str(data.get("decision") or "").strip().upper()
        approve = decision == "APPROVE"
    else:
        approve = bool(approve)
    try:
        confidence = int(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    reason = str(data.get("reason") or data.get("reasoning") or "")
    return {"approve": approve, "confidence": confidence, "reason": reason, "source": "openai"}


def _enhanced_ai_fail_closed_enabled() -> bool:
    return os.getenv("ENHANCED_AI_FAIL_CLOSED", "true").lower() in ("1", "true", "yes")


def _enhanced_ai_heuristic_fallback_enabled() -> bool:
    return os.getenv("ENHANCED_AI_HEURISTIC_FALLBACK", "false").lower() in ("1", "true", "yes")


def _enhanced_ai_fail_closed_result(
    *,
    min_conf: int,
    min_bps: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "approved": False,
        "approve": False,
        "confidence": 0,
        "reason": reason,
        "min_confidence_required": min_conf,
        "min_net_profit_bps": min_bps,
        "source": "fail_closed",
    }


def _build_enhanced_ai_prompt(enriched: dict[str, Any], *, min_bps: int) -> str:
    return render_prompt(
        "enhanced_approve",
        min_bps=min_bps,
        max_vol=_max_volatility_bps_for_ai(),
        signal_json=dumps_json(enriched),
    )


async def _call_enhanced_ai_model(prompt: str) -> str:
    """Low-temperature OpenAI call for the enhanced gate (modern AsyncOpenAI API)."""
    client = _get_openai_client()
    if client is None:
        raise RuntimeError("OPENAI_API_KEY missing or placeholder")

    model = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
    temperature = float(os.getenv("ENHANCED_AI_TEMPERATURE", "0"))
    max_tokens = int(os.getenv("ENHANCED_AI_MAX_TOKENS", "200"))

    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    content = response.choices[0].message.content
    return (content or "").strip()


async def enhanced_ai_approve_decision(
    signal: dict[str, Any],
    *,
    min_confidence: int | None = None,
) -> dict[str, Any]:
    """
    Conservative CEX-DEX / arb gate using OpenAI JSON output.

    Returns ``{"approved": bool, "approve": bool, "confidence": int, "reason": str, ...}``.
    Default: fail-closed (reject) on API/parse/timeout errors.
    """
    enriched = _enrich_signal_for_enhanced_ai(signal)
    min_conf = _ai_approve_min_confidence_floor(min_confidence)

    from src.ai.ensemble_scorer import passes_real_fills_ml_gate

    ml_ok, ml_prob, ml_reason = passes_real_fills_ml_gate(enriched)
    if not ml_ok:
        pct = int(round((ml_prob or 0.0) * 100))
        return {
            "approved": False,
            "approve": False,
            "confidence": pct,
            "reason": ml_reason,
            "min_confidence_required": min_conf,
            "ml_prob": ml_prob,
            "source": "real_fills_lgbm",
        }
    min_bps = int(enriched.get("min_net_profit_bps") or _min_net_profit_bps())
    fail_closed = _enhanced_ai_fail_closed_enabled()
    use_heuristic = _enhanced_ai_heuristic_fallback_enabled()
    parsed: dict[str, Any] | None = None

    if _get_openai_client() is None:
        if fail_closed and not use_heuristic:
            return _enhanced_ai_fail_closed_result(
                min_conf=min_conf,
                min_bps=min_bps,
                reason="openai_client_unavailable",
            )
        parsed = _heuristic_enhanced_approve(enriched, min_conf)
    else:
        prompt = _build_enhanced_ai_prompt(enriched, min_bps=min_bps)
        try:
            try:
                timeout_sec = float(os.getenv("ENHANCED_AI_APPROVE_TIMEOUT_SEC", "8.0"))
            except (TypeError, ValueError):
                timeout_sec = 8.0
            raw = await asyncio.wait_for(
                _call_enhanced_ai_model(prompt),
                timeout=max(1.0, timeout_sec),
            )
            parsed = _parse_enhanced_ai_response(raw)
            if parsed is None:
                raise ValueError("enhanced_ai JSON parse failed")
        except Exception as exc:
            logger.warning("enhanced_ai_approve: %s (%s)", type(exc).__name__, exc)
            if fail_closed and not use_heuristic:
                return _enhanced_ai_fail_closed_result(
                    min_conf=min_conf,
                    min_bps=min_bps,
                    reason=f"error_{type(exc).__name__}",
                )
            parsed = _heuristic_enhanced_approve(enriched, min_conf)

    if parsed is None:
        return _enhanced_ai_fail_closed_result(
            min_conf=min_conf,
            min_bps=min_bps,
            reason="no_decision",
        )

    approved = bool(parsed.get("approve")) and int(parsed.get("confidence", 0)) >= min_conf
    return {
        "approved": approved,
        "approve": bool(parsed.get("approve")),
        "confidence": int(parsed.get("confidence", 0)),
        "reason": parsed.get("reason", ""),
        "min_confidence_required": min_conf,
        "min_net_profit_bps": min_bps,
        "source": parsed.get("source", "openai"),
    }


@dataclass
class AiApproval:
    approve: bool
    confidence: int
    reason: str = ""


async def get_ai_approval(
    *,
    signal_type: str = "cex_dex",
    gross_bps: float = 0.0,
    cex_mid: float = 0.0,
    jup_price: float = 0.0,
    size_usdc_micro: int = 0,
    **extra: Any,
) -> AiApproval:
    """Structured AI gate for ``CexDexCycle`` (prompted via ``get_ai_decision``)."""
    from src.strategies.cex_dex_core import cex_dex_ai_min_confidence

    signal: dict[str, Any] = {
        "strategy": signal_type,
        "gross_bps": round(float(gross_bps), 2),
        "cex_mid": cex_mid,
        "cex_price": cex_mid,
        "jupiter_usdc_per_sol": jup_price,
        "jupiter_price": jup_price,
        "size_usdc_micro": size_usdc_micro,
        "size_usdc": size_usdc_micro,
        **extra,
    }
    min_conf = cex_dex_ai_min_confidence()

    from src.ai.ensemble_scorer import passes_real_fills_ml_gate

    ml_ok, ml_prob, ml_reason = passes_real_fills_ml_gate(signal)
    if not ml_ok:
        pct = int(round((ml_prob or 0.0) * 100))
        return AiApproval(approve=False, confidence=pct, reason=ml_reason)

    try:
        result = await get_ai_decision(signal, strategy=signal_type)
    except Exception as exc:
        logger.warning("get_ai_approval fail-closed: %s", exc)
        return AiApproval(approve=False, confidence=0, reason=str(exc))

    approved = bool(result.get("approve")) and int(result.get("confidence") or 0) >= min_conf
    return AiApproval(
        approve=approved,
        confidence=int(result.get("confidence") or 0),
        reason=str(result.get("reason") or ""),
    )


async def enhanced_ai_approve(
    signal: dict[str, Any],
    *,
    min_confidence: int | None = None,
) -> bool:
    """
    Elite risk-officer gate: approve only if model says yes and confidence >= floor.

    Uses ``settings.MIN_NET_PROFIT_BPS`` / ``settings.AI_APPROVE_MIN_CONFIDENCE`` (overridable via env).
    Safe fail-closed: returns ``False`` on API, parse, or timeout errors unless heuristic fallback is on.
    """
    min_conf = _ai_approve_min_confidence_floor(min_confidence)
    try:
        result = await enhanced_ai_approve_decision(signal, min_confidence=min_conf)
        return bool(result.get("approved"))
    except Exception as exc:
        logger.warning("enhanced_ai_approve fail-closed: %s", exc)
        return False


# ====================== 1. TRADE DECISION MAKING ======================
async def evaluate_trade_decision(opportunity_data: dict) -> dict:
    """High-value: Evaluate risk & approve/reject trade"""
    prompt = render_prompt(
        "trade_decision",
        opportunity_json=dumps_json(opportunity_data),
        cex_prices_json=dumps_json(opportunity_data.get("cex_prices", {})),
    )
    response = await ask_openai(prompt, temperature=0.28)
    try:
        return json.loads(response)
    except:
        return {"decision": "REJECT", "confidence": 0, "reasoning": "JSON parse failed"}


async def evaluate_cex_dex_arb(opportunity: dict) -> dict:
    """CEX vs Jupiter (oracle uses Backpack → Bybit → OKX → KuCoin; execution on Backpack only)."""
    prompt = render_prompt("cex_dex_arb", opportunity_json=dumps_json(opportunity))
    response = await ask_openai(prompt, temperature=0.22)
    try:
        return json.loads(response)
    except Exception:
        return {"decision": "REJECT", "confidence": 0, "reasoning": "JSON parse failed"}


async def evaluate_collateral_swap(opportunity: dict) -> dict:
    """Rate-arb style collateral swap evaluation (JSON-only model response)."""
    prompt = f"""
You are an expert Solana rate-arbitrage trader. Prefer **rare, high-conviction** collateral swaps over frequent small edges.

Only APPROVE when spread / execution risk looks strongly favorable; reject noisy or API-driven APY glitches.

Opportunity:
{json.dumps(opportunity, indent=2)}

Return ONLY JSON:
{{
  "decision": "APPROVE" or "REJECT",
  "confidence": 0-100,
  "reasoning": "short explanation",
  "suggested_flash_amount": number,
  "max_slippage_bps": number,
  "risk_level": "LOW/MEDIUM/HIGH"
}}
"""
    response = await ask_openai(prompt, temperature=0.28)
    try:
        return json.loads(response)
    except Exception:
        return {"decision": "REJECT", "confidence": 0}


# ====================== 2. DYNAMIC TOKEN DISCOVERY ======================
async def discover_new_tokens(seed_symbols_or_mints: list) -> list:
    """Optional helper: suggest tokens given a seed list (not wired into main loop)."""
    prompt = f"""
Known symbols/mints for context: {seed_symbols_or_mints}

Suggest 5-8 Solana tokens (meme or utility) that have good liquidity and momentum right now.
Return ONLY a JSON array of objects:
[{{"mint": "address", "symbol": "TICKER", "reason": "short reason"}}]
"""
    response = await ask_openai(prompt, temperature=0.8)
    try:
        return json.loads(response)
    except:
        return []


# ====================== 3. SENTIMENT & NEWS ANALYSIS ======================
async def analyze_sentiment(token_mint: str, token_symbol: str) -> dict:
    """Analyze Twitter / news sentiment"""
    prompt = f"""
Analyze current market sentiment for {token_symbol} ({token_mint}) on Solana.
Consider recent tweets, hype, rug risk, community strength, and whale activity.
Return JSON:
{{
  "sentiment": "BULLISH / BEARISH / NEUTRAL",
  "score": -100 to 100,
  "key_factors": ["factor1", "factor2"],
  "recommendation": "short advice"
}}
"""
    response = await ask_openai(prompt, temperature=0.6)
    try:
        return json.loads(response)
    except:
        return {"sentiment": "NEUTRAL", "score": 0}


# ====================== 4. RISK MANAGEMENT & SIZING ======================
async def calculate_position_size(opportunity: dict, wallet_balance: int) -> dict:
    """Decide safe position size and leverage"""
    prompt = f"""
Wallet balance: {wallet_balance} lamports (SOL-native lamports; flash routes may ignore wallet USDC).

Sizing stance: **Quality over size** — recommend modest effective risk unless the opportunity is exceptional.

Opportunity: {json.dumps(opportunity)}

Return ONLY JSON:
{{
  "recommended_amount_lamports": number,
  "percentage_of_balance": float,
  "max_slippage_bps": number,
  "should_use_flash_loan": true/false,
  "risk_score": 1-10
}}
"""
    response = await ask_openai(prompt, temperature=0.3)
    try:
        return json.loads(response)
    except:
        return {"recommended_amount_lamports": 50_000_000}


# ====================== 5. STRATEGY IMPROVEMENT ======================
async def suggest_strategy_improvement(daily_performance: dict) -> str:
    """Daily optimization note covering liquidation, collateral, backrun, and CEX-DEX lanes."""
    reviewed = await daily_strategy_review(daily_performance)
    if reviewed:
        return reviewed

    prompt = AIPrompts.get_daily_strategy_review_prompt(daily_performance)
    return await ask_openai(prompt, temperature=0.55, max_tokens=1100)


async def maybe_daily_strategy_improvement(
    state_path: Path,
    daily_performance: dict,
) -> str | None:
    """
    Run suggest_strategy_improvement at most once per UTC day.
    Persists state_path with first line YYYY-MM-DD.
    """
    try:
        from src.monitoring.near_miss_log import load_near_miss_summary_for_daily_review

        near_summary = load_near_miss_summary_for_daily_review()
        if near_summary.get("count"):
            daily_performance = {**daily_performance, "cex_dex_near_misses": near_summary}
    except Exception as exc:
        logger.debug("near_miss summary skipped: %s", exc)

    try:
        cal_path = Path("backtest_results/ml_confidence_calibration.json")
        if cal_path.is_file():
            daily_performance = {
                **daily_performance,
                "ml_calibration": json.loads(cal_path.read_text(encoding="utf-8")),
            }
    except Exception as exc:
        logger.debug("ml calibration skipped: %s", exc)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path.is_file():
        first_line = next(
            (
                ln.strip()
                for ln in state_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ),
            "",
        )
        if first_line == today:
            return None

    advice = await suggest_strategy_improvement(daily_performance)
    if not advice or advice.startswith("Error calling OpenAI"):
        return None

    state_path.write_text(f"{today}\n\n{advice}", encoding="utf-8")
    logger.info("Daily strategy improvement written (%s)", state_path)
    return advice


def _reconcile_brain_scores(scores: dict, snapshot: dict) -> dict[str, float]:
    """
    Clamp model/heuristic scores to what the snapshot actually supports.

    Prevents idle backrun (webhook enabled but no large swap) from blocking CEX-DEX.
    """
    from src.strategies.brain_signals import (
        backrun_signal_present,
        cex_dex_gross_bps_from_snapshot,
        collateral_signal_present,
        liquidation_signal_present,
    )

    out: dict[str, float] = {}
    for k in _STRATEGY_KEYS:
        try:
            out[k] = float(scores.get(k, 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0.0

    if not backrun_signal_present(snapshot):
        out["backrun"] = min(out["backrun"], 12.0)
    if not collateral_signal_present(snapshot):
        out["collateral_swap"] = min(out["collateral_swap"], 12.0)
    if not liquidation_signal_present(snapshot):
        out["liquidation"] = min(out["liquidation"], 12.0)

    cx = snapshot.get("cex_dex") or {}
    gross = cex_dex_gross_bps_from_snapshot(snapshot)
    if bool(cx.get("active")):
        try:
            net = float(cx.get("net_bps") or cx.get("spread_bps_net") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        out["cex_dex"] = max(out["cex_dex"], min(96.0, 55.0 + net))
    elif gross is not None and gross > 0:
        try:
            net = float(cx.get("net_bps") or cx.get("spread_bps_net") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        floor = min(48.0, 18.0 + gross + max(0.0, net) / 2.0)
        out["cex_dex"] = max(out["cex_dex"], floor)

    return out


def heuristic_strategy_scores(snapshot: dict) -> dict:
    """Deterministic fallback when OpenAI is offline or JSON parse fails."""
    scores = {k: 12.0 if k != "liquidation" else 15.0 for k in _STRATEGY_KEYS}
    scores["liquidation"] = 15.0
    scores["collateral_swap"] = 15.0
    scores["backrun"] = 15.0
    scores["cex_dex"] = 12.0
    scores["dex_cex_reverse"] = 10.0

    liq = snapshot.get("liquidation_best") or {}
    try:
        pu = float(liq.get("profit_usdc") or 0.0)
    except (TypeError, ValueError):
        pu = 0.0
    if pu >= 50.0:
        scores["liquidation"] = min(95, 42 + min(50, int(pu)))
    elif pu >= 5.0:
        scores["liquidation"] = 35 + min(45, int(pu * 3))

    col = snapshot.get("collateral_best") or {}
    try:
        spread = float(col.get("spread_bps") or 0.0)
    except (TypeError, ValueError):
        spread = 0.0
    if spread >= 300.0:
        scores["collateral_swap"] = min(92, 38 + int(spread / 25.0))
    elif spread >= 150.0:
        scores["collateral_swap"] = 34 + int(spread / 30.0)

    from src.strategies.brain_signals import backrun_signal_present

    br = snapshot.get("backrun") or {}
    if backrun_signal_present(snapshot):
        try:
            amt = int(br.get("amount_micro") or 0)
        except (TypeError, ValueError):
            amt = 0
        scores["backrun"] = min(92, 72 + int(amt / 10_000_000))
    elif bool(br.get("enabled")):
        scores["backrun"] = 18

    cx = snapshot.get("cex_dex") or {}
    gross = cex_dex_gross_bps_from_snapshot(snapshot)
    weak_lane = preferred_lane_when_weak_cex_dex(snapshot)
    if weak_lane:
        scores["cex_dex"] = 11
        scores[weak_lane] = max(scores[weak_lane], 78)
    elif bool(cx.get("active")):
        try:
            net = float(cx.get("spread_bps_net") or cx.get("net_bps") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        scores["cex_dex"] = min(96, 62 + int(net))
    elif gross is not None and gross > 0:
        try:
            net = float(cx.get("spread_bps_net") or cx.get("net_bps") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        scores["cex_dex"] = min(45, 20 + int(gross) + int(max(0.0, net) / 2))
    else:
        scores["cex_dex"] = 11
    from src.strategies.brain_signals import _weak_cex_dex_gross_threshold_bps

    weak_gross = _weak_cex_dex_gross_threshold_bps()
    if gross is not None and gross < weak_gross and not weak_lane:
        scores["cex_dex"] = min(scores["cex_dex"], 20)

    rev = snapshot.get("dex_cex_reverse") or {}
    from src.strategies.brain_signals import dex_cex_reverse_signal_present

    if dex_cex_reverse_signal_present(snapshot):
        try:
            rev_gross = float(rev.get("gross_bps") or 0.0)
            rev_net = float(rev.get("net_bps") or 0.0)
        except (TypeError, ValueError):
            rev_gross, rev_net = 0.0, 0.0
        scores["dex_cex_reverse"] = min(94, 58 + int(rev_gross) + int(max(0.0, rev_net) / 2))

    scores = _reconcile_brain_scores(scores, snapshot)
    best_strategy, _ = pick_best_strategy_with_priority(scores, snapshot)
    # Confidence reflects winning lane raw score (not biased adjusted value).
    conf_lane = (
        best_strategy if best_strategy != "none" else max(_STRATEGY_KEYS, key=lambda k: scores[k])
    )
    return {
        "scores": scores,
        "best_strategy": best_strategy,
        "confidence": min(100, int(scores[conf_lane])),
        "reasoning": "heuristic_snapshot_scores+strategy_priority",
        "source": "heuristic",
    }


async def score_strategies_cycle(
    snapshot: dict,
    wallet_balance_lamports: int = 0,
) -> dict:
    """
    Score liquidation, collateral_swap, backrun, and cex_dex for one bot cycle and pick a favorite.
    Uses OpenAI when configured; falls back to heuristic_strategy_scores.
    """
    fallback = heuristic_strategy_scores(snapshot)
    if _get_openai_client() is None:
        return fallback

    prio = ",".join(_parse_strategy_priority_order())
    snapshot_ctx = {
        **(snapshot or {}),
        "cex_prices": (snapshot or {}).get("cex_prices", {}),
    }
    prompt = render_prompt(
        "strategy_cycle",
        priority_order=prio,
        snapshot_json=dumps_json(snapshot_ctx),
        wallet_lamports=wallet_balance_lamports,
    )
    raw = await ask_openai(prompt, temperature=0.2, max_tokens=700)
    parsed = _parse_json_dict(raw)
    if isinstance(parsed, dict) and isinstance(parsed.get("scores"), dict):
        parsed["source"] = "openai"
        model_scores: dict[str, float] = {}
        for k in _STRATEGY_KEYS:
            try:
                model_scores[k] = float((parsed["scores"] or {}).get(k, 0) or 0)
            except (TypeError, ValueError):
                model_scores[k] = 0.0
        model_scores = _reconcile_brain_scores(model_scores, snapshot)
        picked, _ = pick_best_strategy_with_priority(model_scores, snapshot)
        parsed["best_strategy"] = picked
        parsed["scores"] = model_scores
        if picked == "none":
            parsed["confidence"] = 0
        else:
            parsed["confidence"] = min(100, int(model_scores[picked]))
        return parsed

    logger.warning("score_strategies_cycle: bad JSON from model; using heuristic")
    return fallback


# ====================== 6. ERROR DEBUGGING ======================
async def debug_transaction_error(error_message: str, tx_signature: str = None) -> str:
    """Explain why a transaction failed"""
    prompt = f"""
Solana transaction failed.
Error: {error_message}
Tx: {tx_signature or "Not provided"}

Explain what went wrong in simple terms and give fix suggestions.
"""
    return await ask_openai(prompt, temperature=0.5)


def passes_ai_numeric_quality_gate(opportunity: dict) -> tuple[bool, str]:
    """
    Optional extra filters when env is set (profit_pct / est_net_pct on opportunity payloads).
    Empty env vars disable each check.
    """
    raw_g = (os.getenv("AI_APPROVE_MIN_PROFIT_PCT") or "").strip()
    if raw_g:
        try:
            need = float(raw_g)
            got = opportunity.get("profit_pct")
            if got is not None and float(got) < need:
                return False, f"profit_pct_below_{need}"
        except (TypeError, ValueError):
            pass

    raw_n = (os.getenv("AI_APPROVE_MIN_EST_NET_PCT") or "").strip()
    if raw_n:
        try:
            need = float(raw_n)
            got = opportunity.get("est_net_pct")
            if got is None:
                pass
            elif float(got) < need:
                return False, f"est_net_pct_below_{need}"
        except (TypeError, ValueError):
            pass

    return True, "ok"


# ====================== MAIN AI AGENT WRAPPER ======================
async def ai_agent_decide(
    opportunity: dict,
    wallet_balance: int,
    *,
    min_confidence: int | None = None,
):
    """Full decision pipeline"""
    logger.info(
        "[%s] AI agent evaluating opportunity…", datetime.now().isoformat(timespec="seconds")
    )

    risk = await calculate_position_size(opportunity, wallet_balance)

    use_enhanced = os.getenv("ENHANCED_AI_APPROVE", "true").lower() in ("1", "true", "yes")

    async def _invoke_ai_evaluator() -> dict:
        if opportunity.get("strategy") == "collateral_swap_rate_arb":
            from src.core.ai_decision import enhanced_ai_approve

            eff_min = min_confidence
            if eff_min is None:
                try:
                    eff_min = int(os.getenv("COLLATERAL_AI_MIN_CONFIDENCE", "55"))
                except (TypeError, ValueError):
                    eff_min = 55
            approved, confidence = await enhanced_ai_approve(
                opportunity, min_conf=int(eff_min)
            )
            return {
                "decision": "APPROVE" if approved else "REJECT",
                "confidence": int(confidence),
                "reasoning": "enhanced_ai_approve",
            }
        if opportunity.get("strategy") == "cex_dex_arb" and use_enhanced:
            verdict = await enhanced_ai_approve_decision(
                opportunity,
                min_confidence=min_confidence,
            )
            action = "APPROVE" if verdict.get("approved") else "REJECT"
            return {
                "decision": action,
                "confidence": verdict.get("confidence", 0),
                "reasoning": verdict.get("reason", ""),
                "enhanced_ai": verdict,
            }
        if opportunity.get("strategy") == "cex_dex_arb":
            return await evaluate_cex_dex_arb(opportunity)
        return await evaluate_trade_decision(opportunity)

    try:
        try:
            timeout_sec = float(os.getenv("AI_AGENT_DECIDE_TIMEOUT_SEC", "8.0"))
        except (TypeError, ValueError):
            timeout_sec = 8.0
        timeout_sec = max(1.0, timeout_sec)
        decision = await asyncio.wait_for(_invoke_ai_evaluator(), timeout=timeout_sec)
    except TimeoutError:
        logger.warning("AI timeout — using heuristic")
        decision = {
            "decision": "APPROVE",
            "confidence": 65,
            "reasoning": "AI timeout — heuristic",
        }

    if not isinstance(decision, dict):
        decision = {"decision": "REJECT", "confidence": 0, "reasoning": "invalid AI response"}

    try:
        confidence = int(decision.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0

    action = str(decision.get("decision") or "REJECT").strip().upper()
    if action not in ("APPROVE", "REJECT"):
        action = "REJECT"

    if min_confidence is not None:
        try:
            min_conf = int(min_confidence)
        except (TypeError, ValueError):
            min_conf = 58
    else:
        try:
            min_conf = int(os.getenv("AI_APPROVE_MIN_CONFIDENCE", "58"))
        except (TypeError, ValueError):
            min_conf = 58
    min_conf = max(0, min(100, min_conf))

    final = action if confidence >= min_conf else "REJECT"

    if final == "APPROVE":
        gate_ok, gate_reason = passes_ai_numeric_quality_gate(opportunity)
        if not gate_ok:
            logger.info(
                "AI numeric quality gate: downgrade APPROVE → REJECT (%s)",
                gate_reason,
            )
            final = "REJECT"

    return {
        "risk_assessment": risk,
        "trade_decision": decision,
        "final_action": final,
    }
