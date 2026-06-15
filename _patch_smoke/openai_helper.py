# openai_helper.py
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logger = logging.getLogger(__name__)

_openai_client: AsyncOpenAI | None = None

# Cycle brain: ordering + optional bias. ``cex_dex`` gets extra bias when snapshot shows it active.
_STRATEGY_KEYS = ("liquidation", "collateral_swap", "backrun", "cex_dex")
_DEFAULT_PRIORITY_ORDER = ("cex_dex", "collateral_swap", "backrun", "liquidation")
_PRIORITY_BIAS_KEYS = frozenset({"collateral_swap", "backrun"})


def _parse_strategy_priority_order() -> list[str]:
    raw = (os.getenv("STRATEGY_PRIORITY_ORDER") or "").strip()
    if not raw:
        return list(_DEFAULT_PRIORITY_ORDER)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p in _STRATEGY_KEYS and p not in seen:
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
    cex_bonus = _cex_dex_brain_priority_bias() if _cex_dex_active(snapshot) else 0.0
    threshold = _strategy_win_threshold()

    adjusted: dict[str, float] = {}
    for k in _STRATEGY_KEYS:
        try:
            v = float(scores.get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if k in _PRIORITY_BIAS_KEYS:
            v += bias_amt
        if k == "cex_dex":
            v += cex_bonus
        adjusted[k] = v

    best_key = max(
        _STRATEGY_KEYS,
        key=lambda k: (adjusted[k], -prio_rank.get(k, 999)),
    )
    if adjusted[best_key] < threshold:
        return "none", adjusted
    return best_key, adjusted


def _get_openai_client() -> AsyncOpenAI | None:
    global _openai_client
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key or key.lower() in ("changeme", "placeholder", "your_key_here"):
        return None
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client


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
    client = _get_openai_client()
    if client is None:
        logger.warning("OPENAI_API_KEY missing or placeholder; skipping model call")
        return "Error calling OpenAI"
    last_err: BaseException | None = None
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
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


# ====================== 1. TRADE DECISION MAKING ======================
async def evaluate_trade_decision(opportunity_data: dict) -> dict:
    """High-value: Evaluate risk & approve/reject trade"""
    prompt = f"""
You are an elite Solana MEV / flash-loan trader optimizing for **few, high-quality trades** (not volume).

Rules:
- APPROVE only when you believe **win probability is high**: edge clearly clears modeled fees/slippage, route looks
  executable, and liquidity/time risk is acceptable.
- REJECT early when anything is marginal, ambiguous, or quote-dependent on thin pools — **being picky is correct**.
- When you APPROVE, be **decisive**: assign **high confidence (typically 78–95)** only for genuinely strong setups.
  Reserve mid confidence for APPROVE sparingly; weak setups must be REJECT or low confidence only.

Opportunity:
{json.dumps(opportunity_data, indent=2)}

CEX Prices: {json.dumps(opportunity_data.get("cex_prices", {}))}
Compare on-chain implied price vs CEX for edge detection.

Return ONLY JSON:
{{
  "decision": "APPROVE" or "REJECT",
  "confidence": 0-100,
  "reasoning": "short explanation",
  "suggested_slippage_bps": number,
  "max_flash_loan_amount": number in lamports,
  "risk_level": "LOW/MEDIUM/HIGH"
}}
"""
    response = await ask_openai(prompt, temperature=0.28)
    try:
        return json.loads(response)
    except:
        return {"decision": "REJECT", "confidence": 0, "reasoning": "JSON parse failed"}


async def evaluate_cex_dex_arb(opportunity: dict) -> dict:
    """CEX (Backpack preferred, Binance fallback) vs Jupiter — wider spreads, larger notionals."""
    prompt = f"""
You evaluate **CEX ↔ DEX** arbitrage on Solana (quoted CEX vs Jupiter), not tight triangular loops.

Core rules:
- Both directions (cex_cheap and dex_cheap) are valid.
- Be decisive but realistic — approve strong edges only.
- Confidence 60-95 for good setups.

Core flow you are gating (payload includes `spread_bps_gross`, `spread_bps_net`, `cost_breakdown`, `direction`):
1. Oracle polls CEX bid/ask + Jupiter quote; detector compares **net** spread vs thresholds after modeled fees,
   withdrawal/bridge, slippage, Kamino flash fee, Jito tip drag.

Direction semantics (payload `direction` is authoritative):
- Direction can be either "cex_cheap" or "dex_cheap". **Both are valid** execution templates when the edge is real.
- For **cex_cheap**: we do **CEX buy + withdraw SOL → Jupiter sell to USDC** (on-chain sells SOL into USDC).
- For **dex_cheap**: **flash borrow USDC → Jupiter buy SOL → CEX sell** (on-chain buys SOL; off-chain sells on CEX).

Strategy context:
- **Spreads**: CEX–DEX dislocations are often **~0.5–3%+** gross (vs very small triangular edges after fees).
  Prefer trusting **`spread_bps_net`** over gross when present.
- **Size**: Notional can scale to **10k–500k+ USDC** (or SOL-equivalent) when edge and liquidity warrant —
  respect `size_usdc` / caps in the payload and liquidity risk.
- **Backpack**: Solana-native CEX — deposits/withdrawals are typically **very fast** (often seconds),
  which matters for round-trip arb.
- **Binance**: Use as **liquidity / fallback** for major pairs when Backpack data is missing or unreliable.

Only APPROVE when the edge clearly covers fees, slippage, transfer latency, and inventory risk; reject stale quotes.

Opportunity:
{json.dumps(opportunity, indent=2)}

Return ONLY JSON:
{{
  "decision": "APPROVE" or "REJECT",
  "confidence": 0-100,
  "reasoning": "short explanation",
  "suggested_slippage_bps": number,
  "risk_level": "LOW/MEDIUM/HIGH"
}}
"""
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
    prompt = f"""
You advise a single-operator Solana bot with four lanes:
1) Kamino liquidation hunting,
2) Collateral / borrow-rate swap arb,
3) Helius-fed Jupiter SWAP backruns bundled via Jito,
4) CEX-DEX (Backpack/Binance vs Jupiter) when spreads are active.

Performance snapshot (may be sparse):
{json.dumps(daily_performance, indent=2)}

Give concise, actionable improvements: risk controls, sizing, when to disable a lane, RPC/Jupiter/Jito tips.
"""
    return await ask_openai(prompt, temperature=0.55, max_tokens=1100)


async def maybe_daily_strategy_improvement(
    state_path: Path,
    daily_performance: dict,
) -> str | None:
    """
    Run suggest_strategy_improvement at most once per UTC day.
    Persists state_path with first line YYYY-MM-DD.
    """
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


def heuristic_strategy_scores(snapshot: dict) -> dict:
    """Deterministic fallback when OpenAI is offline or JSON parse fails."""
    scores = {
        "liquidation": 15,
        "collateral_swap": 15,
        "backrun": 15,
        "cex_dex": 12,
    }

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

    br = snapshot.get("backrun") or {}
    if bool(br.get("enabled")):
        scores["backrun"] = 58 if br.get("pipeline_active") else 44

    cx = snapshot.get("cex_dex") or {}
    if bool(cx.get("active")):
        try:
            net = float(cx.get("spread_bps_net") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        scores["cex_dex"] = min(96, 62 + int(net))
    else:
        scores["cex_dex"] = 11

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
    prompt = f"""
Score four Solana execution lanes for THIS cycle using the snapshot (fields may be null).

Lanes:
- liquidation — Kamino unhealthy obligations (bonus-style payoff).
- collateral_swap — borrow-rate / collateral migration arb.
- backrun — mempool/webhook-triggered Jito bundle behind large Jupiter swaps.
- cex_dex — CEX (Backpack/Binance) vs Jupiter dislocation; snapshot field ``cex_dex`` includes ``active``,
  ``spread_bps_net``, ``spread_bps_gross``, venue, direction when probed.

Priority policy (when edges are comparable or ambiguous): prefer lanes earlier in this order:
  {prio}

**Critical:** If ``snapshot["cex_dex"].active`` is true (actionable CEX-DEX edge after modeled costs),
give **cex_dex** the highest score (typically 88–98) unless another lane shows an overwhelmingly stronger,
immediate edge with concrete numbers in the snapshot.

Collateral and backrun should generally rank higher than liquidation unless liquidation shows a clearly stronger edge.

Snapshot:
{json.dumps(snapshot_ctx, indent=2)}

Approx wallet lamports (SOL native balance): {wallet_balance_lamports}

Return ONLY JSON:
{{
  "scores": {{
    "liquidation": <0-100>,
    "collateral_swap": <0-100>,
    "backrun": <0-100>,
    "cex_dex": <0-100>
  }},
  "best_strategy": "liquidation" | "collateral_swap" | "backrun" | "cex_dex" | "none",
  "confidence": <0-100>,
  "reasoning": "<one short sentence>"
}}
"""
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
        picked, _ = pick_best_strategy_with_priority(model_scores, snapshot)
        parsed["best_strategy"] = picked
        parsed["scores"] = model_scores
        if picked == "none":
            try:
                parsed["confidence"] = min(100, max(0, int(parsed.get("confidence") or 0)))
            except (TypeError, ValueError):
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
    if opportunity.get("strategy") == "collateral_swap_rate_arb":
        decision = await evaluate_collateral_swap(opportunity)
    elif opportunity.get("strategy") == "cex_dex_arb":
        decision = await evaluate_cex_dex_arb(opportunity)
    else:
        decision = await evaluate_trade_decision(opportunity)

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

    final = action if confidence > min_conf else "REJECT"

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
