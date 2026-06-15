from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class StrategyBrain:
    """Lightweight strategy selector shim used by main.py.

    This minimal implementation always selects `cex_dex`. It exists to
    restore the historical `src.utils.ai_brain` import while a richer
    decision process is available in `src.utils.ai`.
    """

    def __init__(self) -> None:
        pass

    async def select_best_strategy(self) -> str:
        # TODO: integrate with src.utils.ai.pick_best_strategy_with_priority
        return "cex_dex"
