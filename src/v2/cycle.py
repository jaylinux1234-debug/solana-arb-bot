"""
One v2 cycle: detect → USDC → gate → execute → log.

Production wiring (replaces tutorial ``V2Cycle`` sketch):
  ``V2ReverseLane`` — detection, sizing, ``execute()`` (inventory preflight inside)
  ``InventoryManager`` — via ``v2_lane.inventory`` (deposit + gated Backpack swap)
  ``run_cycle()`` — called from ``src.v2.main`` poll loop
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.core.circuit_breaker import circuit_breaker
from src.core.wallet import check_global_safety
from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.v2.attempt_log import append_attempt
from src.v2.config import V2Config
from src.v2.dex_cex_reverse import V2ReverseLane
from src.strategies.evaluate_roundtrip import (
    roundtrip_min_net_bps,
    should_execute_roundtrip,
)
from src.v2.gates import check_roundtrip_quote, check_static_gates

logger = logging.getLogger(__name__)

_INVENTORY_BLOCK_REASONS = frozenset(
    {
        "insufficient_backpack_sol",
        "inventory_replenish_failed",
    }
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


async def _maybe_log_inventory_snapshot(
    v2_lane: V2ReverseLane,
    reverse: DexCexReverseStrategy,
    summary: dict[str, Any],
) -> None:
    """Attach Backpack + wallet balances to cycle summary when useful."""
    executed = bool(summary.get("executed"))
    live_fill = bool(summary.get("live_fill"))
    block = str(summary.get("block_reason") or "")
    each_cycle = _env_bool("V2_LOG_INVENTORY_SNAPSHOT_EACH_CYCLE", False)
    on_block = block in _INVENTORY_BLOCK_REASONS
    if not each_cycle and not executed and not live_fill and not on_block:
        return
    if not _env_bool("V2_LOG_INVENTORY_SNAPSHOT", True):
        return
    try:
        snapshot = await v2_lane.inventory.get_inventory_snapshot(
            reverse.backpack,
            wallet_pubkey=reverse.wallet_pubkey,
        )
        summary["inventory_snapshot"] = snapshot
        logger.info("Inventory snapshot | cycle=%s %s", summary.get("cycle"), snapshot)
    except Exception as exc:
        logger.debug("inventory snapshot skipped: %s", exc)


def _apply_execution_result(summary: dict[str, Any], result: dict[str, Any]) -> None:
    """Merge full Jupiter / execution payload into cycle summary for JSONL."""
    status = str(result.get("status") or "")
    summary["execution_status"] = status
    summary["live_fill"] = bool(result.get("live_fill"))
    detail = result.get("jupiter_detail")
    step = result.get("jupiter_step")
    if not step and isinstance(detail, dict):
        step = detail.get("step")
    if step is not None:
        summary["jupiter_step"] = str(step)
    err = result.get("jupiter_error") or result.get("error")
    if err is not None:
        summary["jupiter_error"] = str(err)
    for key in (
        "jupiter_detail",
        "execution_path",
        "realized_usdc",
        "kamino_fallback_from",
    ):
        if result.get(key) is not None:
            summary[key] = result[key]
    if status.startswith("kamino"):
        summary["kamino_status"] = status
    if isinstance(detail, dict) and detail.get("raw_error"):
        summary.setdefault("jupiter_detail", detail)
    tx_sig = result.get("tx_sig") or result.get("jupiter_tx_sig") or result.get("txid")
    if tx_sig:
        summary["tx_sig"] = str(tx_sig)
    if summary["live_fill"]:
        summary["block_reason"] = "filled"
        summary["realized_usdc"] = float(result.get("realized_usdc") or 0)
    elif status == "jupiter_buy_failed":
        summary["block_reason"] = "jupiter_buy_failed"
    elif status == "cex_sell_failed":
        summary["block_reason"] = "cex_sell_failed"
    elif status in ("inventory_replenish_failed", "insufficient_backpack_sol"):
        summary["block_reason"] = status
    elif status == "execution_exception":
        summary["block_reason"] = "execution_exception"
    else:
        summary["block_reason"] = str(
            result.get("block_reason") or status or "execution_failed"
        )
    if result.get("send_path"):
        summary["send_path"] = result["send_path"]


async def can_execute(
    reverse: DexCexReverseStrategy,
    opp: dict[str, Any],
    cfg: V2Config,
) -> tuple[bool, str, float]:
    """Roundtrip + risk gates after signal detection."""
    if not reverse.risk.can_trade(int(opp.get("size_usdc_micro") or 0)):
        return False, "risk_blocked", 0.0

    model_ok, cost = should_execute_roundtrip(
        opp,
        wallet_sol=float(opp.get("wallet_sol") or 0.0),
        cex_sol=float(opp.get("cex_sol") or 0.0),
    )
    opp["roundtrip_model_net_bps"] = cost.net_bps
    opp["roundtrip_model_cost_bps"] = cost.total_cost_bps
    opp["roundtrip_breakdown"] = cost.breakdown
    if not model_ok:
        min_net = roundtrip_min_net_bps()
        return False, f"roundtrip_model_net_below_{min_net:g}", cost.net_bps

    if opp.get("roundtrip_passed"):
        rt_net = float(
            opp.get("roundtrip_net_bps")
            or opp.get("roundtrip_model_net_bps")
            or cost.net_bps
        )
        return True, str(opp.get("roundtrip_reason") or "roundtrip_ok"), rt_net

    return await check_roundtrip_quote(reverse.jupiter, opp, cfg)


async def run_cycle(
    reverse: DexCexReverseStrategy,
    cfg: V2Config,
    *,
    cycle_id: int,
    lane: V2ReverseLane | None = None,
) -> dict[str, Any]:
    """Run a single v2 cycle. Returns summary dict for metrics."""
    v2_lane = lane or V2ReverseLane(reverse, cfg)
    summary: dict[str, Any] = {
        "cycle": cycle_id,
        "lane": "dex_cex_reverse",
        "executed": False,
        "live_fill": False,
        "block_reason": "",
    }

    logger.info("=== V2 Cycle #%s ===", cycle_id)

    if not check_global_safety() or circuit_breaker.should_pause():
        summary["block_reason"] = "safety_blocked"
        append_attempt(cfg.attempts_log, summary)
        return summary

    opportunity = await v2_lane.detect_dex_cheap_signal()
    probe = v2_lane.last_cycle_probe()
    if probe:
        summary.update(
            {
                k: probe[k]
                for k in (
                    "spread_direction",
                    "cex_bid",
                    "jup_price",
                    "gross_bps",
                    "net_bps",
                    "vol_pct",
                    "min_gross_bps",
                    "min_net_bps",
                    "size_usdc",
                )
                if k in probe
            }
        )
    if not opportunity:
        summary["block_reason"] = str(probe.get("block_reason") or "no_signal")
        logger.debug(
            "No executable signal | reason=%s gross=%.2f direction=%s",
            summary["block_reason"],
            float(summary.get("gross_bps") or 0),
            probe.get("spread_direction", "?"),
        )
        append_attempt(cfg.attempts_log, summary)
        return summary

    gross = float(opportunity.get("gross_bps") or 0)
    net = float(opportunity.get("net_bps") or 0)
    summary["gross_bps"] = gross
    summary["net_bps"] = net
    summary["vol_pct"] = float(opportunity.get("vol_pct") or 0)
    summary["event"] = "STRONG_DEX_CHEAP_SIGNAL"
    summary["block_reason"] = "STRONG_DEX_CHEAP_SIGNAL"
    for key in (
        "roundtrip_net_bps",
        "roundtrip_reason",
        "roundtrip_jup_price",
        "roundtrip_gross_bps",
        "trade_usdc",
    ):
        if key in opportunity:
            summary[key] = opportunity[key]
    append_attempt(cfg.attempts_log, {**summary, "executed": False})

    sized, usdc_reason, onchain_usdc = await v2_lane.prepare_execution(opportunity)
    summary["onchain_usdc"] = onchain_usdc
    if sized:
        path = str(sized.get("execution_path") or "wallet_usdc")
        summary["execution_path"] = path
        if path == "wallet_usdc" and cfg.kamino_wallet_first:
            summary["sizing_mode"] = "WALLET_FIRST"
        elif path == "kamino_flash":
            summary["sizing_mode"] = "KAMINO_FLASH"
    if not sized:
        summary["block_reason"] = usdc_reason or "insufficient_usdc"
        summary["onchain_usdc"] = onchain_usdc
        if usdc_reason == "insufficient_usdc":
            summary["replenish_hint"] = "V2_AUTO_WITHDRAW_USDC_FROM_CEX"
        elif usdc_reason in _INVENTORY_BLOCK_REASONS:
            summary["block_reason"] = usdc_reason
            summary["replenish_hint"] = (
                "ENABLE_BACKPACK_AUTO_REPLENISH,V2_AUTO_DEPOSIT_SOL_TO_CEX,"
                "V2_BACKPACK_SWAP_SOL_ENABLED"
            )
        logger.info(
            "Cycle #%s | USDC preflight failed | on_chain=$%.2f reason=%s",
            cycle_id,
            onchain_usdc,
            summary["block_reason"],
        )
        append_attempt(cfg.attempts_log, summary)
        if str(summary.get("block_reason") or "") in _INVENTORY_BLOCK_REASONS:
            await _maybe_log_inventory_snapshot(v2_lane, reverse, summary)
        return summary

    static_ok, static_reason, _ = check_static_gates(sized, cfg)
    if not static_ok:
        summary["block_reason"] = static_reason
        append_attempt(cfg.attempts_log, summary)
        return summary

    exec_opp = {**opportunity, **sized}
    ok, reason, rt_net = await can_execute(reverse, exec_opp, cfg)
    summary["roundtrip_net_bps"] = rt_net
    summary["trade_usdc"] = float(sized.get("size_usdc") or 0)
    for key in (
        "roundtrip_jup_price",
        "roundtrip_gross_bps",
        "roundtrip_reason",
        "roundtrip_model_net_bps",
        "roundtrip_model_cost_bps",
        "roundtrip_breakdown",
    ):
        if key in exec_opp:
            summary[key] = exec_opp[key]
    if not ok:
        summary["block_reason"] = reason
        logger.info(
            "Cycle #%s | gross=%.2f | roundtrip_net=%.2f | skip=%s",
            cycle_id,
            gross,
            rt_net,
            reason,
        )
        append_attempt(cfg.attempts_log, summary)
        return summary

    logger.info(
        "EXECUTING_REVERSE_ARB | path=%s gross=%.2f net=%.2f rt_net=%.2f "
        "onchain_usdc=%.2f trade_usdc=%.2f",
        summary.get("execution_path", "wallet_usdc"),
        gross,
        net,
        rt_net,
        onchain_usdc,
        summary["trade_usdc"],
    )
    summary["executed"] = True
    summary["execution_status"] = "EXECUTING"
    summary["event"] = "EXECUTING_REVERSE_ARB"
    summary["block_reason"] = "EXECUTING_REVERSE_ARB"
    append_attempt(cfg.attempts_log, {**summary})

    result: dict[str, Any] | None = None
    try:
        result = await v2_lane.execute(sized)
        if not isinstance(result, dict):
            result = {
                "status": "execution_failed",
                "live_fill": False,
                "block_reason": "execution_empty_result",
                "jupiter_error": "execute_returned_non_dict",
            }
    except Exception as exc:
        logger.error("Execution exception: %s", exc, exc_info=True)
        result = {
            "status": "execution_exception",
            "live_fill": False,
            "block_reason": "execution_exception",
            "jupiter_step": "exception",
            "jupiter_error": str(exc),
            "error": str(exc),
            "execution_path": summary.get("execution_path", "wallet_usdc"),
        }
    finally:
        if result is not None:
            _apply_execution_result(summary, result)
            if summary.get("live_fill"):
                logger.info(
                    "JUPITER_SWAP_SUCCESS | FILL_SUCCESS tx=%s usdc=%.4f",
                    summary.get("tx_sig", ""),
                    float(summary.get("realized_usdc") or 0),
                )
                try:
                    from src.monitoring.capital_delta import log_capital_delta

                    await log_capital_delta(
                        "reverse_live_fill",
                        strategy="dex_cex_reverse",
                        extra={
                            "realized_usdc": float(summary.get("realized_usdc") or 0),
                            "tx_sig": summary.get("tx_sig"),
                            "cycle": cycle_id,
                        },
                    )
                except Exception as cap_exc:
                    logger.debug("capital_delta after fill: %s", cap_exc)
            else:
                logger.warning(
                    "EXECUTION_FAILED | reason=%s jupiter_step=%s jupiter_error=%s tx_sig=%s",
                    summary.get("block_reason", ""),
                    summary.get("jupiter_step", ""),
                    summary.get("jupiter_error", ""),
                    summary.get("tx_sig", ""),
                )
        try:
            append_attempt(cfg.attempts_log, summary)
        except Exception as exc:
            logger.error("v2 terminal attempt log failed | cycle=%s: %s", cycle_id, exc)
        await _maybe_log_inventory_snapshot(v2_lane, reverse, summary)
    return summary


class V2Cycle:
    """
    Production v2 trading engine.

    Wraps ``run_cycle`` + ``InventoryManager`` startup. Used by ``src.v2.main``.
    """

    def __init__(
        self,
        reverse: DexCexReverseStrategy,
        cfg: V2Config,
        lane: V2ReverseLane,
        *,
        shutdown_event: Any | None = None,
    ) -> None:
        self.reverse = reverse
        self.cfg = cfg
        self.lane = lane
        self.inventory = lane.inventory
        self._shutdown = shutdown_event
        self._cycle_id = 0

    async def run_startup(self) -> None:
        """USDC replenish + Backpack SOL target + inventory snapshot."""
        wallet = self.reverse.wallet_pubkey
        startup_usdc, replenish_note = await self.inventory.replenish_usdc_for_trade(
            self.reverse.backpack,
            self.reverse.jupiter,
            wallet_pubkey=wallet,
        )
        startup_cex_sol = await self.inventory.get_backpack_sol(self.reverse.backpack)
        logger.info(
            "Startup USDC | on_chain=$%.2f note=%s | Backpack SOL=%.6f",
            startup_usdc,
            replenish_note or "ok",
            startup_cex_sol,
        )
        if startup_cex_sol < float(self.inventory.target_backpack_sol):
            await self.inventory.ensure_cex_sol(
                self.inventory.target_backpack_sol,
                self.reverse.backpack,
                wallet_pubkey=wallet,
            )
        try:
            inv_snap = await self.inventory.get_inventory_snapshot(
                self.reverse.backpack,
                wallet_pubkey=wallet,
            )
            logger.info("Startup inventory snapshot: %s", inv_snap)
        except Exception as exc:
            logger.debug("Startup inventory snapshot skipped: %s", exc)

    async def run_one_cycle(self) -> dict[str, Any]:
        """Single detect → size → execute cycle."""
        self._cycle_id += 1
        return await run_cycle(
            self.reverse,
            self.cfg,
            cycle_id=self._cycle_id,
            lane=self.lane,
        )

    async def run_forever(self) -> None:
        """Poll loop until ``shutdown_event`` is set."""
        await self.run_startup()
        logger.info(
            "V2Cycle started | poll=%.1f–%.1fs max_usdc=%.0f reserve_sol=%.2f",
            self.cfg.poll_min_sec,
            self.cfg.poll_max_sec,
            self.cfg.max_trade_usdc,
            float(self.inventory.min_sol_reserve),
        )
        while self._shutdown is None or not self._shutdown.is_set():
            try:
                summary = await self.run_one_cycle()
                if summary.get("live_fill"):
                    logger.info(
                        "V2Cycle fill | cycle=%s net_usdc≈%.4f",
                        summary.get("cycle"),
                        float(summary.get("realized_usdc") or 0),
                    )
                    cooldown = float(
                        os.getenv("LIVE_TRADE_COOLDOWN_SECONDS", "45")
                    )
                    if cooldown > 0:
                        logger.info(
                            "V2Cycle post-fill cooldown | %.0fs",
                            cooldown,
                        )
                        if self._shutdown is None:
                            await asyncio.sleep(cooldown)
                        else:
                            try:
                                await asyncio.wait_for(
                                    self._shutdown.wait(),
                                    timeout=cooldown,
                                )
                                break
                            except TimeoutError:
                                pass
            except Exception as exc:
                logger.error("V2Cycle error: %s", exc, exc_info=True)

            sleep_sec = max(self.cfg.poll_min_sec, self.cfg.poll_max_sec)
            if self._shutdown is None:
                await asyncio.sleep(sleep_sec)
                continue
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_sec)
            except TimeoutError:
                pass
