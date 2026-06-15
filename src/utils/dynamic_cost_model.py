"""
Dynamic Cost Model for CEX-DEX Arbitrage
Learns from past executions to improve profitability.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class DynamicCostModel:
    def __init__(self, state_path: str = "logs/cost_model_state.json"):
        self.state_path = Path(state_path)
        self.history: list[dict] = []
        self._load()

    def _load(self):
        if self.state_path.exists():
            try:
                with open(self.state_path) as f:
                    data = json.load(f)
                    self.history = data.get("executions", [])
            except Exception:
                self.history = []

    def record_execution(self, trade_data: dict):
        """Record real execution outcome."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "gross_bps": trade_data.get("gross_bps"),
            "net_bps": trade_data.get("net_bps"),
            "realized_net_bps": trade_data.get("realized_net_bps"),
            "size_usdc": trade_data.get("size_usdc_micro", 0) / 1_000_000,
            "slippage_bps": trade_data.get("slippage_bps", 0),
            "jito_tip": trade_data.get("jito_tip", 0),
        }
        self.history.append(entry)
        
        # Keep last 200 trades
        if len(self.history) > 200:
            self.history = self.history[-200:]
        
        self._save()
        self._update_model()

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"executions": self.history, "updated": datetime.utcnow().isoformat()}, f, indent=2)

    def _update_model(self):
        """Update base cost estimates from history."""
        if len(self.history) < 10:
            return
        
        recent = [t for t in self.history if 
                 datetime.fromisoformat(t["timestamp"]) > datetime.utcnow() - timedelta(hours=48)]
        
        if recent:
            avg_slippage = sum(t["slippage_bps"] for t in recent) / len(recent)
            logger.info("DynamicCostModel updated | avg_slippage=%.2f (from %d trades)", 
                       avg_slippage, len(recent))

    def get_recommended_cost_buffer(self, size_usdc: int) -> float:
        """Return recommended cost buffer for current conditions."""
        base = 22.0  # conservative default
        if self.history:
            recent_slippage = sum(t["slippage_bps"] for t in self.history[-30:]) / min(30, len(self.history))
            base = max(18.0, recent_slippage * 1.1)
        return base


# Global instance
cost_model = DynamicCostModel()