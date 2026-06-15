"""
src/utils/ai_prompts.py
Versioned, structured AI prompts for trading decisions.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from src.config.settings import get_settings

PROMPT_VERSION = os.getenv("AI_PROMPT_VERSION", "2026-05-25-v2")


class AIPrompts:
    """Centralized prompt templates for different strategies."""

    @staticmethod
    def get_system_prompt() -> str:
        """Base system prompt - strict risk & profitability focused."""
        return """You are an elite Solana CEX-DEX arbitrage AI.
You are extremely conservative and risk-averse.
Only approve trades when there is clear edge with safety margin.

Core Rules:
- Never approve if net expected profit < 0.06%
- Prefer higher confidence over more trades
- Always consider slippage, fees, withdrawal latency, and inventory risk
- Reject if market volatility is high
"""

    @staticmethod
    def get_cex_dex_prompt(opp: dict[str, Any]) -> str:
        """CEX-DEX specific prompt."""
        settings = get_settings()

        size_usdc = opp.get("size_usdc")
        if size_usdc is None and opp.get("size_usdc_micro") is not None:
            try:
                size_usdc = float(opp["size_usdc_micro"]) / 1_000_000.0
            except (TypeError, ValueError):
                size_usdc = 0
        elif size_usdc is not None:
            try:
                size_usdc = float(size_usdc)
                if size_usdc > 1_000:
                    size_usdc = size_usdc / 1_000_000.0
            except (TypeError, ValueError):
                size_usdc = 0
        else:
            size_usdc = 0

        cex_price = opp.get("cex_price", opp.get("cex_mid", "N/A"))
        jupiter_price = opp.get("jupiter_price", opp.get("jupiter_usdc_per_sol", "N/A"))
        gross_bps = opp.get("gross_bps", opp.get("spread_bps_gross", 0))
        net_bps = opp.get("net_bps", opp.get("spread_bps_net", 0))

        return f"""Current Time: {datetime.now(UTC).isoformat()}

CEX-DEX Arbitrage Opportunity:

- CEX Price     : {cex_price} USDC/SOL
- Jupiter Price : {jupiter_price} USDC/SOL
- Gross Spread  : {gross_bps} bps
- Net Spread    : {net_bps} bps (after costs)
- Trade Size    : {size_usdc:.2f} USDC
- Confidence Required: ≥ {settings.ai_approve_min_confidence}%

Analyze and respond in JSON format only:

{{
  "approve": true/false,
  "confidence": 0-100,
  "reason": "short explanation",
  "risk_factors": ["list of concerns"],
  "suggested_size_usdc": number (or null)
}}
"""

    @staticmethod
    def get_liquidation_prompt(data: dict[str, Any]) -> str:
        return f"""Liquidation Opportunity Analysis:

{json.dumps(data, indent=2, default=str)}

Decide whether to execute liquidation. Respond in strict JSON format."""

    @staticmethod
    def get_daily_strategy_review_prompt(stats: dict[str, Any]) -> str:
        """Daily performance review prompt."""
        near = stats.get("cex_dex_near_misses") or {}
        hourly = near.get("hourly_patterns") or {}
        slippage_note = ""
        if hourly.get("likely_high_slippage_hours_utc"):
            slippage_note = (
                "\n\nNear-miss hourly pattern (UTC): "
                f"{json.dumps(hourly.get('likely_high_slippage_hours_utc'), default=str)}. "
                "Suggest tighter slippage or higher gross gates in those hours."
            )
        ml_cal = stats.get("ml_calibration") or {}
        cal_note = ""
        if ml_cal.get("suggested_ai_approve_min_confidence"):
            cal_note = (
                f"\n\nML suggests AI_APPROVE_MIN_CONFIDENCE="
                f"{ml_cal['suggested_ai_approve_min_confidence']} "
                f"(precision@floor={ml_cal.get('precision_at_floor', 'n/a')})."
            )
        return f"""Review today's trading performance and suggest improvements:

Stats: {json.dumps(stats, indent=2, default=str)}{slippage_note}{cal_note}

Provide strategic insights and risk adjustments for tomorrow.
Call out time-of-day slippage patterns and whether CEX_DEX_MIN_GROSS_SPREAD_BPS / AI confidence should shift."""

    @staticmethod
    def get_version() -> str:
        return PROMPT_VERSION


def get_ai_decision_prompt(strategy: str, data: dict[str, Any]) -> dict[str, str]:
    prompts = AIPrompts()

    if strategy == "cex_dex":
        return {
            "system": prompts.get_system_prompt(),
            "user": prompts.get_cex_dex_prompt(data),
        }
    if strategy == "liquidation":
        return {
            "system": prompts.get_system_prompt(),
            "user": prompts.get_liquidation_prompt(data),
        }
    return {
        "system": prompts.get_system_prompt(),
        "user": json.dumps(data, indent=2, default=str),
    }


# ---------------------------------------------------------------------------
# Legacy template registry (used by src.utils.ai ``render_prompt``)
# ---------------------------------------------------------------------------

PROMPTS: dict[str, dict[str, str]] = {
    "enhanced_approve": {
        "v1": """Elite arb risk officer (prompt {version}). {system}

Approve ONLY if:
- Net edge > {min_bps} bps after all costs (fees, slippage, Jito tip, withdrawal)
- Volatility acceptable (reject if > {max_vol} bps)

Signal: {signal_json}

Return JSON only: {{"approve": true/false, "confidence": int, "reason": "brief", "risk_factors": []}}""",
        PROMPT_VERSION: """Elite arb risk officer (prompt {version}). {system}

Approve ONLY if:
- Net edge > {min_bps} bps after all costs (fees, slippage, Jito tip, withdrawal)
- Volatility acceptable (reject if > {max_vol} bps)

Signal: {signal_json}

Return JSON only: {{"approve": true/false, "confidence": int, "reason": "brief", "risk_factors": []}}""",
    },
    "trade_decision": {
        "v1": """You are an elite Solana MEV / flash-loan trader (prompt {version}). Optimize for few, high-quality trades.

Rules:
- APPROVE only when win probability is high: edge clears fees/slippage, route executable, liquidity/time risk acceptable.
- REJECT marginal or ambiguous setups.
- High confidence (78–95) only for genuinely strong APPROVE setups.

Opportunity:
{opportunity_json}

CEX Prices: {cex_prices_json}

Return ONLY JSON:
{{
  "decision": "APPROVE" or "REJECT",
  "confidence": 0-100,
  "reasoning": "short explanation",
  "suggested_slippage_bps": number,
  "max_flash_loan_amount": number,
  "risk_level": "LOW/MEDIUM/HIGH"
}}""",
    },
    "cex_dex_arb": {
        "v1": """{system}

Opportunity:
{opportunity_json}

Return ONLY JSON:
{{
  "decision": "APPROVE" or "REJECT",
  "confidence": 0-100,
  "reasoning": "short explanation",
  "suggested_slippage_bps": number,
  "risk_level": "LOW/MEDIUM/HIGH"
}}""",
        PROMPT_VERSION: """{system}

Opportunity:
{opportunity_json}

Return ONLY JSON:
{{
  "approve": true/false,
  "confidence": 0-100,
  "reason": "short explanation",
  "risk_factors": ["list of concerns"],
  "suggested_size_usdc": number (or null)
}}""",
    },
    "strategy_cycle": {
        "v1": """Score four Solana lanes for THIS cycle (prompt {version}).

Lanes: liquidation, collateral_swap, backrun, cex_dex.
Priority order when comparable: {priority_order}

If cex_dex.active and gross/net bps >= 60, favor cex_dex (88–98) unless another lane is far stronger.
If CEX-DEX gross < 60 bps, prefer liquidation (profit_usdc >= 5) else collateral_swap.

Snapshot:
{snapshot_json}

Wallet lamports: {wallet_lamports}

Return ONLY JSON with scores, best_strategy, confidence, reasoning.""",
    },
}


def prompt_version() -> str:
    return AIPrompts.get_version()


def dumps_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def render_prompt(name: str, *, version: str | None = None, **kwargs: Any) -> str:
    """Render a named prompt template; ``version`` defaults to ``PROMPT_VERSION``."""
    family = PROMPTS.get(name)
    if not family:
        raise KeyError(f"Unknown prompt family: {name}")

    ver = (version or PROMPT_VERSION).strip()
    template = family.get(ver) or family.get("v1") or next(iter(family.values()))
    kwargs.setdefault("version", ver)

    if name in ("enhanced_approve", "cex_dex_arb") and "system" not in kwargs:
        kwargs["system"] = AIPrompts.get_system_prompt()

    if name == "cex_dex_arb" and ver == PROMPT_VERSION and "opportunity_json" in kwargs:
        try:
            opp = json.loads(kwargs["opportunity_json"])
            if isinstance(opp, dict):
                return AIPrompts.get_cex_dex_prompt(opp)
        except json.JSONDecodeError:
            pass

    return template.format(**kwargs)
