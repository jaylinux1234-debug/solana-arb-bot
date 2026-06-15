#!/usr/bin/env python3
"""One-shot: copy root modules into src/ and rewrite local imports."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FILE_MOVES: list[tuple[str, str]] = [
    ("security.py", "src/core/security.py"),
    ("circuit_breaker.py", "src/core/circuit_breaker.py"),
    ("connection.py", "src/core/rpc.py"),
    ("wallet_safety.py", "src/core/wallet.py"),
    ("tokens.py", "src/core/tokens.py"),
    ("config.py", "src/config/settings_legacy.py"),
    ("cex_executor.py", "src/cex/executor.py"),
    ("cex_price_feed.py", "src/cex/price_feed.py"),
    ("cex_dex_inventory.py", "src/cex/inventory.py"),
    ("inventory_reconcile.py", "src/cex/inventory_reconcile.py"),
    ("get_jupiter_quote.py", "src/dex/quote.py"),
    ("jupiter_executor.py", "src/dex/jupiter.py"),
    ("jupiter_quotes.py", "src/dex/quotes.py"),
    ("kamino_helper.py", "src/dex/kamino.py"),
    ("kamino_api.py", "src/dex/kamino_api.py"),
    ("cex_dex_core.py", "src/strategies/cex_dex_core.py"),
    ("cex_dex_strategy.py", "src/strategies/cex_dex.py"),
    ("cex_dex_arb.py", "src/strategies/cex_dex_arb.py"),
    ("cex_dex_flash_arb_fixed.py", "src/strategies/cex_dex_flash.py"),
    ("solana_cex_dex_flash_bot.py", "src/strategies/cex_dex_flash_bot.py"),
    ("liquidation_bot.py", "src/strategies/liquidation.py"),
    ("collateral_swap_executor.py", "src/strategies/collateral_swap.py"),
    ("openai_helper.py", "src/utils/ai.py"),
    ("strategy_cycle_signals.py", "src/strategies/brain_signals.py"),
    ("pnl_confidence.py", "src/strategies/brain_pnl.py"),
    ("jito_helper.py", "src/execution/jito.py"),
    ("helius_webhook.py", "src/execution/helius.py"),
    ("arbitrage_detector.py", "src/execution/arbitrage.py"),
    ("mempool_watcher.py", "src/execution/mempool.py"),
    ("redis_client.py", "src/utils/redis.py"),
    ("health_api.py", "src/utils/health.py"),
    ("health_server.py", "src/monitoring/health_server.py"),
    ("price_health.py", "src/monitoring/price_health.py"),
]

IMPORT_MAP: dict[str, str] = {
    "security": "src.core.security",
    "circuit_breaker": "src.core.circuit_breaker",
    "connection": "src.core.rpc",
    "wallet_safety": "src.core.wallet",
    "tokens": "src.core.tokens",
    "config": "src.config.settings",
    "cex_executor": "src.cex.executor",
    "cex_price_feed": "src.cex.price_feed",
    "cex_dex_inventory": "src.cex.inventory",
    "inventory_reconcile": "src.cex.inventory_reconcile",
    "get_jupiter_quote": "src.dex.quote",
    "jupiter_executor": "src.dex.jupiter",
    "jupiter_quotes": "src.dex.quotes",
    "kamino_helper": "src.dex.kamino",
    "kamino_api": "src.dex.kamino_api",
    "cex_dex_core": "src.strategies.cex_dex_core",
    "cex_dex_strategy": "src.strategies.cex_dex",
    "cex_dex_arb": "src.strategies.cex_dex_arb",
    "cex_dex_flash_arb_fixed": "src.strategies.cex_dex_flash",
    "solana_cex_dex_flash_bot": "src.strategies.cex_dex_flash_bot",
    "liquidation_bot": "src.strategies.liquidation",
    "collateral_swap_executor": "src.strategies.collateral_swap",
    "openai_helper": "src.utils.ai",
    "strategy_cycle_signals": "src.strategies.brain_signals",
    "pnl_confidence": "src.strategies.brain_pnl",
    "jito_helper": "src.execution.jito",
    "helius_webhook": "src.execution.helius",
    "arbitrage_detector": "src.execution.arbitrage",
    "mempool_watcher": "src.execution.mempool",
    "redis_client": "src.utils.redis",
    "health_api": "src.utils.health",
    "health_server": "src.monitoring.health_server",
    "price_health": "src.monitoring.price_health",
}

FROM_RE = re.compile(r"^(\s*)from\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+import\s+", re.M)
IMPORT_RE = re.compile(r"^(\s*)import\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", re.M)


def rewrite_imports(text: str) -> str:
    def from_sub(m: re.Match) -> str:
        mod = m.group(2)
        return m.group(0).replace(f"from {mod} ", f"from {IMPORT_MAP.get(mod, mod)} ", 1)

    def imp_sub(m: re.Match) -> str:
        mod = m.group(2)
        if mod not in IMPORT_MAP:
            return m.group(0)
        return f"{m.group(1)}import {IMPORT_MAP[mod]} as {mod}"

    text = FROM_RE.sub(from_sub, text)
    text = IMPORT_RE.sub(imp_sub, text)
    return text


def main() -> None:
    for _, dest in FILE_MOVES:
        Path(ROOT / dest).parent.mkdir(parents=True, exist_ok=True)

    for src_rel, dest_rel in FILE_MOVES:
        src = ROOT / src_rel
        dest = ROOT / dest_rel
        if not src.is_file():
            print(f"skip missing {src_rel}")
            continue
        content = src.read_text(encoding="utf-8")
        dest.write_text(rewrite_imports(content), encoding="utf-8")
        print(f"copied {src_rel} -> {dest_rel}")

    for pkg in (
        "src",
        "src/config",
        "src/core",
        "src/cex",
        "src/dex",
        "src/strategies",
        "src/execution",
        "src/utils",
        "src/monitoring",
        "tests",
    ):
        init = ROOT / pkg / "__init__.py"
        if not init.is_file():
            init.write_text('"""Package."""\n', encoding="utf-8")


if __name__ == "__main__":
    main()
