# src/ai/decision.py
"""
AI Decision Engine - High Win-Rate Scoring for CEX-DEX Arbitrage
Uses GPT-4o-mini with structured output for reliable confidence scoring
"""

import json
import logging
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from src.config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class AIScorer:
    """AI-powered opportunity scorer with PnL memory"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=self._load_openai_key(),
            timeout=10.0
        )
        self.history_path = "logs/pnl_confidence_window.json"
        self.recent_pnl: list[dict] = self._load_pnl_history()

    def _load_openai_key(self) -> str | None:
        key = getattr(settings, 'OPENAI_API_KEY', None)
        if not key and hasattr(settings, 'OPENAI_API_KEY_FILE'):
            try:
                with open(settings.OPENAI_API_KEY_FILE) as f:
                    key = f.read().strip()
            except Exception:
                pass
        if not key:
            logger.warning("OPENAI_API_KEY not configured - AI fallback enabled")
        return key

    def _load_pnl_history(self) -> list[dict]:
        """Load recent trade PnL for context"""
        try:
            import json
            with open(self.history_path) as f:
                data = json.load(f)
                # Keep last 72-96 hours
                cutoff = datetime.now() - timedelta(hours=settings.AI_PNL_CONFIDENCE_WINDOW_HOURS)
                return [t for t in data if datetime.fromisoformat(t['timestamp']) > cutoff]
        except:
            return []

    def _save_pnl(self, profit_usdc: float, net_bps: float):
        """Append trade result to history"""
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "profit_usdc": profit_usdc,
                "net_bps": net_bps,
                "confidence": "N/A"
            }
            self.recent_pnl.append(entry)
            # Keep only last 200 entries
            if len(self.recent_pnl) > 200:
                self.recent_pnl = self.recent_pnl[-200:]

            import json
            with open(self.history_path, 'w') as f:
                json.dump(self.recent_pnl, f, indent=2)
        except:
            pass

    async def score(
        self,
        cex_price: float,
        jup_price: float,
        size_usdc: float,
        net_bps: float
    ) -> float:
        """
        Score opportunity confidence (0-100)
        Higher = more likely to be profitable
        """
        if not self.client.api_key:
            # Fallback heuristic when no API key
            base_conf = min(92, max(65, net_bps * 1.4))
            return round(base_conf, 1)

        # Build context from recent trades
        pnl_summary = "No recent trades"
        if self.recent_pnl:
            wins = sum(1 for t in self.recent_pnl if t['profit_usdc'] > 0)
            win_rate = (wins / len(self.recent_pnl)) * 100 if self.recent_pnl else 0
            avg_profit = sum(t['profit_usdc'] for t in self.recent_pnl) / len(self.recent_pnl)
            pnl_summary = f"Last {len(self.recent_pnl)} trades: {win_rate:.1f}% win rate, avg ${avg_profit:.2f} profit"

        prompt = f"""
You are an elite Solana CEX-DEX arbitrage risk engine.

Current Opportunity:
- CEX Price (Backpack): ${cex_price:.4f}
- Jupiter Price: ${jup_price:.4f}
- Gross Spread: {((cex_price - jup_price)/jup_price*10000):.1f} bps
- Net Spread after costs: {net_bps:.1f} bps
- Trade Size: ${size_usdc:.1f}k USDC

Recent Performance: {pnl_summary}

Market Context: Paid RPC + Helius Webhook active. Sweet spot sizing 30k-500k USDC.

Score this opportunity with confidence 0-100.
Consider:
- Spread sustainability
- Current market volatility
- Recent PnL trend
- Slippage & execution risk
- Overall edge quality

Respond with valid JSON only:
{{
  "confidence": 87,
  "reason": "strong spread + positive recent win rate + good size",
  "recommend": true
}}
"""

        try:
            response = await self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a precise, conservative trading AI. Never hallucinate."},
                    {"role": "user", "content": prompt}
                ],
                temperature=settings.ENHANCED_AI_TEMPERATURE,
                max_tokens=180,
                response_format={"type": "json_object"}
            )

            content = response.choices[0].message.content
            result = json.loads(content)

            confidence = float(result.get("confidence", 65))
            confidence = max(50, min(98, confidence))  # clamp

            logger.info(f"AI Score: {confidence}% | Reason: {result.get('reason', '')[:80]}")

            return confidence

        except Exception as e:
            logger.warning(f"AI scoring failed: {e}. Using heuristic fallback.")
            return min(88, max(68, net_bps * 1.35))

    async def record_trade_result(self, profit_usdc: float, net_bps: float, confidence: float):
        """Record outcome for future learning"""
        self._save_pnl(profit_usdc, net_bps)
        logger.info(f"AI Learning: Recorded trade | Profit: ${profit_usdc:.2f} | Confidence was: {confidence}%")


# Global singleton
_ai_scorer: AIScorer | None = None


def get_ai_scorer() -> AIScorer:
    global _ai_scorer
    if _ai_scorer is None:
        _ai_scorer = AIScorer()
    return _ai_scorer