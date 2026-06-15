"""On-chain / CEX inventory helpers for health checks and v2 strategy facades."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_replenish_last_at: float = 0.0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _replenish_cooldown_sec() -> float:
    try:
        return float(os.getenv("V2_USDC_REPLENISH_COOLDOWN_SEC", "120"))
    except (TypeError, ValueError):
        return 120.0


async def get_usdc_balance_async(*, wallet_pubkey: str | None = None) -> float:
    """SPL USDC balance for ``WALLET_PUBKEY``."""
    from src.v2.usdc_manager import USDCManager
    from src.v2.config import V2Config

    cfg = V2Config.from_env()
    mgr = USDCManager(cfg)
    _ = wallet_pubkey
    return float(await mgr.get_available_usdc())


async def get_sol_balance_async(*, wallet_pubkey: str | None = None) -> float:
    """Native SOL balance for fee reserve."""
    from src.core.rpc_config import get_robust_sol_balance

    pk = (wallet_pubkey or os.getenv("WALLET_PUBKEY") or "").strip()
    return float(await get_robust_sol_balance(pk or None))


def get_usdc_balance() -> float:
    """Sync helper (health / scripts) — runs async fetch in a fresh loop if needed."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_usdc_balance_async())
    if loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, get_usdc_balance_async()).result()
    return loop.run_until_complete(get_usdc_balance_async())


def get_sol_balance() -> float:
    """Sync SOL balance helper."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_sol_balance_async())
    if loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, get_sol_balance_async()).result()
    return loop.run_until_complete(get_sol_balance_async())


async def replenish_if_low(*, force: bool = False) -> dict[str, Any]:
    """
    Withdraw USDC from Backpack when on-chain balance is below ``V2_MIN_USDC_BALANCE``.

    Used after collateral fills and during reconciliation to recycle capital.
    """
    global _replenish_last_at

    from src.config.settings import get_settings
    from src.cex.backpack import get_backpack_client
    from src.v2.config import V2Config
    from src.v2.usdc_manager import USDCManager

    settings = get_settings()
    min_usdc = float(getattr(settings, "V2_MIN_USDC_BALANCE", 15.0) or 15.0)
    usdc = await get_usdc_balance_async()
    result: dict[str, Any] = {
        "onchain_usdc": round(usdc, 4),
        "min_usdc": min_usdc,
        "replenished": False,
        "note": "",
    }

    if usdc >= min_usdc and not force:
        result["note"] = "ok"
        return result

    now = time.monotonic()
    if not force and (now - _replenish_last_at) < _replenish_cooldown_sec():
        result["note"] = "cooldown"
        logger.debug(
            "USDC replenish skipped (cooldown) | on_chain=$%.2f min=$%.2f",
            usdc,
            min_usdc,
        )
        return result

    if not _env_bool("V2_AUTO_WITHDRAW_USDC_FROM_CEX", True):
        result["note"] = "auto_withdraw_disabled"
        return result

    logger.warning(
        "Low on-chain USDC ($%.2f < $%.2f). Triggering Backpack withdraw...",
        usdc,
        min_usdc,
    )

    cfg = V2Config.from_env()
    mgr = USDCManager(cfg)
    backpack = get_backpack_client()
    try:
        updated, note = await mgr.replenish_usdc_for_trade(backpack, jupiter=None)
        _replenish_last_at = now
        result["replenished"] = updated > usdc
        result["onchain_usdc"] = round(updated, 4)
        result["note"] = note or ("backpack_withdraw" if updated > usdc else "withdraw_no_change")
        if result["replenished"]:
            logger.info(
                "USDC replenish complete | $%.2f -> $%.2f (%s)",
                usdc,
                updated,
                result["note"],
            )
    except Exception as exc:
        logger.warning("USDC replenish failed: %s", exc)
        result["note"] = str(exc)[:200]
    finally:
        close = getattr(backpack, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                pass

    return result


async def reconcile_inventory(force: bool = False) -> dict[str, float | bool | str]:
    """
    Snapshot wallet + Backpack balances; auto-replenish when on-chain USDC is low.
    """
    from src.config.settings import get_settings
    from src.cex.backpack import BackpackClient

    settings = get_settings()
    min_usdc = float(getattr(settings, "V2_MIN_USDC_BALANCE", 15.0) or 15.0)

    chain_usdc = await get_usdc_balance_async()
    chain_sol = await get_sol_balance_async()

    cex_usdc = 0.0
    cex_sol = 0.0
    backpack = BackpackClient(settings)
    try:
        cex_usdc = float(await backpack.get_balance("USDC"))
        cex_sol = float(await backpack.get_balance("SOL"))
    except Exception as exc:
        logger.debug("Backpack balance read failed: %s", exc)

    low_usdc = chain_usdc < min_usdc
    snapshot: dict[str, float | bool | str] = {
        "chain_usdc": round(chain_usdc, 4),
        "chain_sol": round(chain_sol, 6),
        "cex_usdc": round(cex_usdc, 4),
        "cex_sol": round(cex_sol, 6),
        "low_usdc": low_usdc,
        "replenish_note": "",
    }

    if low_usdc:
        logger.warning(
            "Low on-chain USDC | $%.2f < min $%.2f (cex_usdc=$%.2f)",
            chain_usdc,
            min_usdc,
            cex_usdc,
        )
        auto = _env_bool("ENABLE_BACKPACK_AUTO_REPLENISH", True)
        if auto or force:
            repl = await replenish_if_low(force=force)
            snapshot["replenish_note"] = str(repl.get("note") or "")
            if repl.get("replenished"):
                snapshot["chain_usdc"] = float(repl.get("onchain_usdc") or chain_usdc)
                snapshot["low_usdc"] = float(snapshot["chain_usdc"]) < min_usdc

    try:
        from src.monitoring.metrics import set_inventory_reconcile_ok

        set_inventory_reconcile_ok(not bool(snapshot["low_usdc"]))
    except Exception:
        pass

    logger.info(
        "Inventory reconciled | chain_usdc=$%.2f cex_usdc=$%.2f sol=%.4f low=%s",
        float(snapshot["chain_usdc"]),
        cex_usdc,
        chain_sol,
        snapshot["low_usdc"],
    )
    return snapshot
