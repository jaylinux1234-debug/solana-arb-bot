"""GMGN-style smart money copy trading lane."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.strategies.meme_lanes_config import get_smart_money_settings
from src.strategies.position_manager import position_manager

logger = logging.getLogger(__name__)


@dataclass
class SmartWallet:
    address: str
    win_rate: float
    pnl_mult: float
    style: str = "sniper"


def _parse_tracked_wallets(raw: str) -> list[SmartWallet]:
    """Parse SMART_MONEY_TRACKED_WALLETS=addr:win:pnl:style,addr2:..."""
    out: list[SmartWallet] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":")
        if len(bits) < 1:
            continue
        addr = bits[0].strip()
        if not addr:
            continue
        try:
            win = float(bits[1]) if len(bits) > 1 else 0.7
            pnl = float(bits[2]) if len(bits) > 2 else 3.0
        except ValueError:
            win, pnl = 0.7, 3.0
        style = bits[3].strip() if len(bits) > 3 else "sniper"
        out.append(SmartWallet(address=addr, win_rate=win, pnl_mult=pnl, style=style))
    return out


async def fetch_top_wallets(
    *,
    min_win_rate: float = 0.65,
    min_pnl_mult: float = 2.5,
) -> list[SmartWallet]:
    """Load configured wallets; optional Helius leaderboard stub."""
    cfg = get_smart_money_settings()
    wallets = _parse_tracked_wallets(cfg.tracked_wallets_raw)
    if wallets:
        return [
            w
            for w in wallets
            if w.win_rate >= min_win_rate and w.pnl_mult >= min_pnl_mult
        ]

    # Optional: seed from env file path
    path = (os.getenv("SMART_MONEY_WALLETS_FILE") or "").strip()
    if path and os.path.isfile(path):
        try:
            text = open(path, encoding="utf-8").read()
            wallets = _parse_tracked_wallets(text.replace("\n", ","))
            return [
                w
                for w in wallets
                if w.win_rate >= min_win_rate and w.pnl_mult >= min_pnl_mult
            ]
        except OSError:
            pass
    return []


async def _rpc_get_recent_signatures(wallet: str, limit: int = 8) -> list[str]:
    from src.core.rpc_config import call_with_rpc_fallback

    async def _call(rpc_url: str) -> list[str]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, {"limit": limit}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("result") or []
            return [str(r.get("signature") or "") for r in rows if r.get("signature")]

    try:
        return await call_with_rpc_fallback("default", _call, label="smart_money:sigs")
    except Exception as exc:
        logger.debug("smart_money sig poll failed | wallet=%s err=%s", wallet[:8], exc)
        return []


async def _parse_swap_from_tx(signature: str) -> dict[str, Any] | None:
    from src.core.rpc_config import call_with_rpc_fallback

    async def _call(rpc_url: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                },
            )
            resp.raise_for_status()
            tx = resp.json().get("result")
            if not tx:
                return None
            meta = tx.get("meta") or {}
            pre = {str(b.get("mint")): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                   for b in (meta.get("preTokenBalances") or [])}
            post = {str(b.get("mint")): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                    for b in (meta.get("postTokenBalances") or [])}
            for mint in set(pre) | set(post):
                if mint == "So11111111111111111111111111111111111111112":
                    continue
                delta = float(post.get(mint, 0)) - float(pre.get(mint, 0))
                if delta > 0:
                    return {"token_mint": mint, "is_buy": True, "token_delta": delta}
                if delta < 0:
                    return {"token_mint": mint, "is_buy": False, "token_delta": abs(delta)}
            return None

    try:
        return await call_with_rpc_fallback("default", _call, label="smart_money:tx")
    except Exception:
        return None


class SmartMoneyCopy:
    def __init__(self) -> None:
        self.config = get_smart_money_settings()
        self.tracked_wallets: dict[str, dict[str, Any]] = {}
        self.active_copies: dict[str, float] = {}
        self._seen_sigs: dict[str, set[str]] = {}
        self._last_wallet_refresh = 0.0

    async def update_smart_wallets(self) -> None:
        cfg = self.config
        top = await fetch_top_wallets(
            min_win_rate=cfg.min_win_rate,
            min_pnl_mult=cfg.min_pnl_mult,
        )
        self.tracked_wallets = {
            w.address: {
                "win_rate": w.win_rate,
                "pnl_mult": w.pnl_mult,
                "style": w.style,
            }
            for w in top
        }
        if top:
            logger.info("smart_money wallets refreshed | count=%d", len(top))

    def _calculate_signal_strength(self, wallet: str) -> float:
        data = self.tracked_wallets.get(wallet) or {}
        win = float(data.get("win_rate") or 0)
        pnl = min(float(data.get("pnl_mult") or 0), 5.0)
        return (win * 0.6 + pnl * 0.4) * 100.0

    def _calculate_copy_size(self, leader_size: float, strength: float) -> float:
        cfg = self.config
        base = min(leader_size * cfg.leader_size_mult, cfg.max_copy_sol)
        return max(0.0, base * (strength / 100.0))

    async def on_new_trade(
        self,
        wallet: str,
        token_mint: str,
        amount_sol: float,
        is_buy: bool,
    ) -> None:
        if wallet not in self.tracked_wallets or not is_buy:
            return

        strength = self._calculate_signal_strength(wallet)
        if strength < self.config.min_copy_confidence:
            logger.debug(
                "smart_money skip low confidence | wallet=%s strength=%.1f",
                wallet[:8],
                strength,
            )
            return

        copy_size = self._calculate_copy_size(amount_sol, strength)
        if copy_size <= 0:
            return

        await self.execute_copy_trade(token_mint, copy_size, is_buy=True, wallet=wallet, strength=strength)

    async def execute_copy_trade(
        self,
        token_mint: str,
        size_sol: float,
        *,
        is_buy: bool,
        wallet: str = "",
        strength: float = 0.0,
    ) -> None:
        cfg = self.config
        if cfg.simulate:
            logger.info(
                "[SIM] smart_money_copy | wallet=%s mint=%s size_sol=%.3f strength=%.1f buy=%s",
                wallet[:8] if wallet else "?",
                token_mint[:12],
                size_sol,
                strength,
                is_buy,
            )
            await position_manager.open_via_execution(token_mint, size_sol, lane="smart_money_copy")
            self.active_copies[token_mint] = size_sol
            return

        if is_buy:
            await position_manager.open_via_execution(token_mint, size_sol, lane="smart_money_copy")
            self.active_copies[token_mint] = size_sol
            logger.info(
                "smart_money_copy executed | wallet=%s mint=%s size_sol=%.3f",
                wallet[:8],
                token_mint[:12],
                size_sol,
            )

    async def poll_wallet_activity(self) -> None:
        for wallet in list(self.tracked_wallets.keys()):
            sigs = await _rpc_get_recent_signatures(wallet)
            seen = self._seen_sigs.setdefault(wallet, set())
            for sig in sigs:
                if not sig or sig in seen:
                    continue
                seen.add(sig)
                if len(seen) > 500:
                    self._seen_sigs[wallet] = set(list(seen)[-200:])
                trade = await _parse_swap_from_tx(sig)
                if trade and trade.get("token_mint"):
                    est_sol = max(0.05, min(self.config.max_copy_sol, 0.25))
                    await self.on_new_trade(
                        wallet,
                        str(trade["token_mint"]),
                        est_sol,
                        bool(trade.get("is_buy")),
                    )


_smart_money: SmartMoneyCopy | None = None


def get_smart_money_copy() -> SmartMoneyCopy:
    global _smart_money
    if _smart_money is None:
        _smart_money = SmartMoneyCopy()
    return _smart_money


async def smart_money_copy_loop(shutdown_event=None) -> None:
    cfg = get_smart_money_settings()
    if not cfg.enabled:
        return

    bot = get_smart_money_copy()
    logger.info(
        "Smart money copy lane started | simulate=%s wallets=%d min_conf=%.0f",
        cfg.simulate,
        len(cfg.tracked_wallets),
        cfg.min_copy_confidence,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            return
        try:
            now = time.monotonic()
            if now - bot._last_wallet_refresh > 300:
                await bot.update_smart_wallets()
                bot._last_wallet_refresh = now
            if bot.tracked_wallets:
                await bot.poll_wallet_activity()
            await position_manager.monitor_positions()
            await asyncio.sleep(cfg.poll_interval_sec)
        except Exception as exc:
            logger.error("smart_money_copy_loop error: %s", exc)
            await asyncio.sleep(5.0)
