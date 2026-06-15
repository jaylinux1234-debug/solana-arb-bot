# src/core/ai_decision.py
import json
import logging

import openai

from src.config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class AISignalValidator:
    def __init__(self):
        self.client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.pnl_window = []
        self.window_path = "logs/pnl_confidence_window.json"

    async def load_history(self):
        try:
            with open(self.window_path) as f:
                data = json.load(f)
                self.pnl_window = data.get("window", [])
        except:
            self.pnl_window = []

    async def save_history(self):
        with open(self.window_path, "w") as f:
            json.dump({"window": self.pnl_window[-50:]}, f)  # Keep last 50

    async def evaluate(self, net_bps: float, cex_price: dict, historical_edge: bool = True) -> int:
        """Multi-factor AI confidence score (0-100)"""
        await self.load_history()

        base_confidence = 75

        # 1. Spread Strength
        if net_bps >= 70:
            base_confidence += 18
        elif net_bps >= 55:
            base_confidence += 12

        # 2. Volatility Filter
        vol = cex_price.get("volatility_bps", 80)
        if vol < 60:
            base_confidence += 8
        elif vol > 140:
            base_confidence -= 15

        # 3. Historical PnL Context
        recent_pnl = sum(self.pnl_window[-8:]) if self.pnl_window else 0
        if recent_pnl < 0:
            base_confidence -= 12

        # 4. GPT-4o Mini Smart Analysis
        try:
            prompt = f"""
            Analyze this CEX-DEX arbitrage opportunity:
            Net spread: {net_bps} bps
            Volatility: {vol} bps
            Recent PnL trend: {recent_pnl}
            Should we execute? Return only confidence score 0-100.
            """
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=10,
                temperature=0.1
            )
            gpt_score = int(response.choices[0].message.content.strip())
            base_confidence = (base_confidence + gpt_score) // 2
        except:
            pass  # Fallback to heuristic

        final_score = max(60, min(98, base_confidence))
        return final_score


async def _get_ai_confidence(opportunity: dict) -> float:
    """Resolve model confidence for an opportunity dict."""
    from src.utils.ai import evaluate_collateral_swap, evaluate_trade_decision

    strategy = str(opportunity.get("strategy") or "")
    if strategy == "collateral_swap_rate_arb":
        decision = await evaluate_collateral_swap(opportunity)
    else:
        decision = await evaluate_trade_decision(opportunity)
    try:
        return float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


async def enhanced_ai_approve(
    opportunity: dict,
    min_conf: int = 62,
) -> tuple[bool, float]:
    """Improved AI gate with heuristic fallback for strong edges."""
    try:
        gross_bps = float(opportunity.get("gross_bps") or 0)
        net_bps = float(opportunity.get("net_bps") or 0)
        confidence = await _get_ai_confidence(opportunity)

        if gross_bps > 25:
            confidence = max(confidence, 68.0)

        if confidence < min_conf and net_bps > 4.5:
            confidence = float(min_conf + 5)

        if confidence >= min_conf:
            return True, confidence

        logger.info(
            "AI reject | conf=%.1f net_bps=%.2f gross_bps=%.2f",
            confidence,
            net_bps,
            gross_bps,
        )
        return False, confidence
    except Exception as exc:
        logger.warning("AI error, using heuristic: %s", exc)
        gross_bps = float(opportunity.get("gross_bps") or 0)
        net_bps = float(opportunity.get("net_bps") or 0)
        ok = net_bps > 3.8 and gross_bps > 12
        return ok, 60.0 if ok else 0.0