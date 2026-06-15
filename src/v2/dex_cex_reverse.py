"""
v2.4.2 reverse lane: dex-cheap detection, roundtrip gate, Jupiter execution.

Wires through ``DexCexReverseStrategy`` (Backpack + Jupiter + Ledger). JSONL logging
is handled by ``src.v2.cycle`` via ``append_attempt`` — not ``log_attempt`` on this class.

Config mapping (your sketch → this module):
  ``v2_max_flash_usdc`` → ``V2Config.max_trade_usdc``
  ``v2_min_gross_bps`` / ``v2_min_net_bps`` → ``min_gross_bps_base`` / ``min_net_bps_base``
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from solders.transaction import VersionedTransaction

from src.core.circuit_breaker import circuit_breaker
from src.core.wallet import check_global_safety
from src.dex.jupiter import SOL_MINT, USDC_MINT
from src.monitoring.metrics import record_trade_execution
from src.strategies.cex_dex_core import analyze_cex_dex_spread
from src.strategies.dex_cex_reverse import DexCexReverseStrategy
from src.utils.price import bps_diff
from src.v2.config import V2Config
from src.v2.cost_model import CostModel, default_cost_model, refresh_cost_model
from src.v2.gates import check_roundtrip_quote, resolve_adaptive_thresholds

# Env-backed singleton; call ``refresh_cost_model(cfg)`` after loading .env in main.
cost_model = default_cost_model()
from src.v2.cex_venues import CexBidAggregator
from src.v2.inventory_manager import InventoryManager
from src.v2.volatility import VolatilityTracker

logger = logging.getLogger(__name__)


def _pnl_log_path() -> Path:
    return Path(os.getenv("V2_PNL_LOG", "logs/v2_pnl.jsonl"))


class V2ReverseLane:
    """Main reverse (dex_cheap) lane: cost model, roundtrip gate, Jupiter + CEX sell."""

    def __init__(
        self,
        reverse: DexCexReverseStrategy,
        cfg: V2Config,
    ) -> None:
        self.reverse = reverse
        self.config = cfg
        self.cost_model = CostModel.from_config(cfg)
        self.vol_tracker = VolatilityTracker(cfg.vol_lookback_min)
        self.inventory = InventoryManager(cfg, backpack=reverse.backpack)
        self.usdc_manager = self.inventory.usdc
        self._cex_bids = CexBidAggregator(reverse.backpack)
        self.logger = logger
        self._last_cycle_probe: dict[str, Any] = {}

    async def get_bid_price(self, symbol: str = "SOL") -> float | None:
        bid, venue = await self._cex_bids.best_bid(symbol)
        if bid and bid > 0:
            if venue and venue != "backpack":
                self._last_cycle_probe.setdefault("cex_bid_venue", venue)
            return float(bid)
        return await self.reverse.get_bid_price(symbol)

    async def get_jupiter_sol_price(
        self,
        size_usdc_micro: int | None = None,
        *,
        cex_bid: float | None = None,
        slippage_bps: int | None = None,
    ) -> float | None:
        micro = int(size_usdc_micro or self.config.probe_usdc_micro)
        slip = (
            slippage_bps
            if slippage_bps is not None
            else self._get_slippage_bps()
        )
        return await self.reverse.get_jupiter_sol_price(
            micro,
            cex_reference=cex_bid,
            slippage_bps=slip,
        )

    @staticmethod
    def calculate_gross_bps(cex_bid: float, jup_price: float) -> float:
        return float(bps_diff(float(jup_price), float(cex_bid)))

    def last_cycle_probe(self) -> dict[str, Any]:
        """Spread snapshot from the most recent ``detect_dex_cheap_signal`` call."""
        return dict(self._last_cycle_probe)

    async def detect_dex_cheap_signal(self) -> dict[str, Any] | None:
        """Detect dex-cheap with adaptive vol gates and structured logging."""
        self._last_cycle_probe = {}
        try:
            size_micro = self.config.max_trade_usdc_micro
            size_usdc = self.config.max_trade_usdc
            cex_bid = await self.get_bid_price("SOL")
            jup_price = await self.get_jupiter_sol_price(
                size_micro,
                cex_bid=cex_bid,
                slippage_bps=self._get_slippage_bps(size_usdc),
            )

            if not cex_bid or cex_bid <= 0 or not jup_price or jup_price <= 0:
                reason = (
                    "bad_jupiter_price"
                    if cex_bid and cex_bid > 0
                    else "missing_quotes"
                )
                self._last_cycle_probe = {
                    "block_reason": reason,
                    "cex_bid": round(float(cex_bid), 4) if cex_bid else None,
                    "jup_price": round(float(jup_price), 4) if jup_price else None,
                    "gross_bps": 0.0,
                    "net_bps": 0.0,
                }
                self.logger.info("NO_SIGNAL: %s", reason)
                return None

            spread = analyze_cex_dex_spread(float(cex_bid), float(jup_price))
            direction = spread.direction if spread else "none"
            gross_bps = 0.0
            if spread:
                gross_bps = self.calculate_gross_bps(float(cex_bid), float(jup_price))
                if gross_bps <= 0:
                    gross_bps = float(spread.spread_bps_abs)

            max_sane_gross = float(os.getenv("V2_MAX_SANE_GROSS_BPS", "200"))
            if gross_bps > max_sane_gross:
                self._last_cycle_probe = {
                    "block_reason": "bad_jupiter_price",
                    "cex_bid": round(float(cex_bid), 4),
                    "jup_price": round(float(jup_price), 4),
                    "gross_bps": round(gross_bps, 3),
                    "net_bps": 0.0,
                }
                self.logger.warning(
                    "BAD_JUPITER_QUOTE | gross_bps=%.2f cap=%.2f jup=%.4f cex=%.4f",
                    gross_bps,
                    max_sane_gross,
                    jup_price,
                    cex_bid,
                )
                return None

            if gross_bps > 15.0:
                mult = float(os.getenv("V2_HIGH_CONF_SIZE_MULT", "1.0"))
                size_usdc = min(25.0, self.config.max_trade_usdc * max(1.0, mult))
                size_micro = int(size_usdc * 1_000_000)
                refreshed = await self.get_jupiter_sol_price(
                    size_micro,
                    cex_bid=cex_bid,
                    slippage_bps=self._get_slippage_bps(size_usdc),
                )
                if refreshed and refreshed > 0:
                    jup_price = float(refreshed)
                    if spread:
                        gross_bps = self.calculate_gross_bps(float(cex_bid), jup_price)
                        if gross_bps <= 0:
                            gross_bps = float(spread.spread_bps_abs)
                self.logger.info(
                    "HIGH_CONFIDENCE_SIZE | gross_bps=%.2f size_usdc=%.2f size_micro=%s",
                    gross_bps,
                    size_usdc,
                    size_micro,
                )

            self.vol_tracker.update(float(cex_bid))
            vol_pct = self.vol_tracker.get_volatility_pct()
            inventory_healthy = await self.inventory.is_inventory_healthy(
                self.reverse.backpack,
                wallet_pubkey=self.reverse.wallet_pubkey,
                trade_usdc_micro=size_micro,
                cex_bid=float(cex_bid) if cex_bid else None,
            )
            min_gross, min_net = resolve_adaptive_thresholds(
                self.config,
                vol_pct,
                inventory_healthy,
            )
            size_usdc = size_micro / 1_000_000.0
            wallet_sol = await self.inventory.get_wallet_sol(self.reverse.wallet_pubkey)
            cex_sol = await self.inventory.get_backpack_sol(self.reverse.backpack)
            net_bps = self.cost_model.calculate_net_bps(
                gross_bps,
                size_usdc,
                vol_pct,
                wallet_sol=float(wallet_sol),
                cex_sol=float(cex_sol),
            )

            self._last_cycle_probe = {
                "spread_direction": direction,
                "cex_bid": round(float(cex_bid), 4),
                "jup_price": round(float(jup_price), 4),
                "gross_bps": round(gross_bps, 3),
                "net_bps": round(net_bps, 3),
                "vol_pct": round(vol_pct, 2),
                "min_gross_bps": round(min_gross, 3),
                "min_net_bps": round(min_net, 3),
                "inventory_healthy": inventory_healthy,
                "size_usdc": round(size_usdc, 2),
            }

            self.logger.info(
                "CYCLE_SPREAD | cex_bid=%.4f jup_price=%.4f direction=%s gross_bps=%.3f "
                "net_bps=%.3f vol_pct=%.2f min_gross=%.3f min_net=%.3f inventory_ok=%s",
                cex_bid,
                jup_price,
                direction,
                gross_bps,
                net_bps,
                vol_pct,
                min_gross,
                min_net,
                inventory_healthy,
            )

            if spread is None or spread.direction != "dex_cheap":
                self._last_cycle_probe["block_reason"] = f"not_{direction}"
                self.logger.debug("NO_SIGNAL: direction=%s", direction)
                return None

            if gross_bps < min_gross:
                self._last_cycle_probe["block_reason"] = "gross_below_threshold"
                self.logger.info(
                    "gross_below_threshold | gross=%.3f required=%.3f net=%.3f vol_pct=%.2f",
                    gross_bps,
                    min_gross,
                    net_bps,
                    vol_pct,
                )
                return None

            if net_bps < min_net:
                self._last_cycle_probe["block_reason"] = "net_below_threshold"
                self.logger.info(
                    "net_below_threshold | net=%.3f required=%.3f gross=%.3f vol_pct=%.2f",
                    net_bps,
                    min_net,
                    gross_bps,
                    vol_pct,
                )
                return None

            draft: dict[str, Any] = {
                "symbol": "SOL",
                "pair_label": "SOL/USDC",
                "direction": "dex_cheap",
                "is_dex_cheap": True,
                "gross_bps": gross_bps,
                "net_bps": net_bps,
                "scan_gross_bps": gross_bps,
                "vol_pct": vol_pct,
                "min_gross_bps": min_gross,
                "min_net_bps": min_net,
                "inventory_healthy": inventory_healthy,
                "size_usdc_micro": size_micro,
                "size_usdc": size_usdc,
                "cex_bid": float(cex_bid),
                "jup_price": float(jup_price),
                "wallet_sol": float(wallet_sol),
                "cex_sol": float(cex_sol),
                "path": "dex_cex_reverse",
                "lane": "dex_cex_reverse",
            }
            rt_ok, rt_reason, roundtrip_net = await check_roundtrip_quote(
                self.reverse.jupiter,
                draft,
                self.config,
            )
            self._last_cycle_probe["roundtrip_net_bps"] = round(roundtrip_net, 3)
            if not rt_ok:
                self._last_cycle_probe["block_reason"] = rt_reason
                self.logger.info(
                    "ROUNDTRIP_SKIP | reason=%s scan_gross=%.2f roundtrip_net=%.3f",
                    rt_reason,
                    gross_bps,
                    roundtrip_net,
                )
                return None

            self._last_cycle_probe["block_reason"] = "STRONG_DEX_CHEAP_SIGNAL"
            self._last_cycle_probe["event"] = "STRONG_DEX_CHEAP_SIGNAL"
            self.logger.info(
                "STRONG_DEX_CHEAP_SIGNAL | gross=%.2f net=%.2f roundtrip_net=%.3f "
                "vol_pct=%.2f size_usdc=%.2f cex_bid=%.4f jup=%.4f rt=%s",
                gross_bps,
                net_bps,
                roundtrip_net,
                vol_pct,
                size_usdc,
                cex_bid,
                jup_price,
                rt_reason,
            )
            return {
                **draft,
                "scan_gross": gross_bps,
                "roundtrip_passed": True,
                "roundtrip_reason": rt_reason,
                "roundtrip_net_bps": roundtrip_net,
                "strong_signal": True,
                "event": "STRONG_DEX_CHEAP_SIGNAL",
            }
        except Exception as exc:
            self._last_cycle_probe = {"block_reason": "detect_error", "error": str(exc)}
            self.logger.error("SIGNAL_ERROR | %s", exc, exc_info=True)
            return None

    async def execute_opportunity(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Main execution entry (sketch-compatible name → ``execute``)."""
        return await self.execute(signal)

    async def _should_route_kamino_flash(
        self,
        available: float,
        opportunity: dict[str, Any],
    ) -> bool:
        """Kamino when prefer_flash is on, or auto when USDC/SOL inventory is low."""
        if not self.config.enable_kamino_flash:
            return False
        if self.config.kamino_prefer_flash:
            return True
        if not self.config.kamino_flash_on_low_inventory:
            return False
        if not self.usdc_manager.has_minimum(available):
            return True
        signal_micro = int(
            opportunity.get("size_usdc_micro") or self.config.max_trade_usdc_micro
        )
        cex_bid = opportunity.get("cex_bid")
        healthy = await self.inventory.is_inventory_healthy(
            self.reverse.backpack,
            wallet_pubkey=self.reverse.wallet_pubkey,
            trade_usdc_micro=signal_micro,
            cex_bid=float(cex_bid) if cex_bid else None,
        )
        return not healthy

    def _ensure_cex_sol_context(self, signal: dict[str, Any]) -> dict[str, Any]:
        bid = float(signal.get("cex_bid") or 0)
        return {
            "strong_signal": bool(
                signal.get("strong_signal")
                or signal.get("roundtrip_passed")
                or signal.get("event") == "STRONG_DEX_CHEAP_SIGNAL"
            ),
            "cex_bid": bid if bid > 0 else None,
        }

    async def _ensure_cex_sol_for_signal(
        self,
        sized_draft: dict[str, Any],
        available: float,
    ) -> tuple[dict[str, Any] | None, str]:
        required_sol = self._required_cex_sol_from_signal(sized_draft)
        sol_ok = await self.inventory.ensure_cex_sol(
            required_sol,
            self.reverse.backpack,
            wallet_pubkey=self.reverse.wallet_pubkey,
            **self._ensure_cex_sol_context(sized_draft),
        )
        if sol_ok:
            return {
                **sized_draft,
                "required_cex_sol": float(required_sol),
            }, ""
        cex_sol = await self.inventory.get_backpack_sol(self.reverse.backpack)
        self.logger.error(
            "Inventory replenish failed - skipping trade | cex=%.6f required=%.6f",
            cex_sol,
            float(required_sol),
        )
        return None, "inventory_replenish_failed"

    async def _size_wallet_usdc(
        self,
        opportunity: dict[str, Any],
        available: float,
        signal_micro: int,
    ) -> tuple[dict[str, Any] | None, str]:
        trade_micro = self.usdc_manager.trade_size_micro(available, signal_micro)
        if trade_micro < 1_000_000:
            return None, "trade_size_too_small"
        sized_draft = {
            **opportunity,
            "size_usdc_micro": trade_micro,
            "size_usdc": trade_micro / 1_000_000.0,
            "onchain_usdc": available,
            "execution_path": "wallet_usdc",
        }
        sized, block = await self._ensure_cex_sol_for_signal(sized_draft, available)
        if block:
            return None, block
        self.logger.info(
            "USDC_SIZING | on_chain=$%.2f trade_usdc=%.2f required_cex_sol=%.6f",
            available,
            trade_micro / 1_000_000.0,
            float(sized.get("required_cex_sol") or 0),
        )
        return sized, ""

    async def prepare_execution(
        self,
        opportunity: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, float]:
        """
        Size trade for wallet USDC or Kamino flash borrow.

        Returns (sized_opportunity, block_reason, onchain_usdc).
        """
        available, replenish_note = await self.usdc_manager.replenish_usdc_for_trade(
            self.reverse.backpack,
            self.reverse.jupiter,
            wallet_pubkey=self.reverse.wallet_pubkey,
        )
        if replenish_note:
            self.logger.info("USDC replenish note: %s", replenish_note)
        signal_micro = int(opportunity.get("size_usdc_micro") or self.config.max_trade_usdc_micro)

        use_kamino = await self._should_route_kamino_flash(available, opportunity)
        if use_kamino:
            use_wallet = (
                self.config.kamino_wallet_first
                and self.usdc_manager.has_minimum(available)
            )
            if use_wallet:
                trade_micro = self.usdc_manager.trade_size_micro(
                    available, signal_micro
                )
                if trade_micro >= 1_000_000:
                    sized_draft = {
                        **opportunity,
                        "size_usdc_micro": trade_micro,
                        "size_usdc": trade_micro / 1_000_000.0,
                        "onchain_usdc": available,
                        "execution_path": "wallet_usdc",
                    }
                    sized, block = await self._ensure_cex_sol_for_signal(
                        sized_draft,
                        available,
                    )
                    if block:
                        return None, block, available
                    self.logger.info(
                        "WALLET_FIRST_SIZING | on_chain=$%.2f trade_usdc=$%.2f "
                        "(kamino reserved for low balance)",
                        available,
                        trade_micro / 1_000_000.0,
                    )
                    return sized, "", available

            flash_cap = min(
                signal_micro,
                self.config.max_trade_usdc_micro,
                self.config.kamino_flash_amount_usdc_micro,
            )
            trade_micro = flash_cap
            if trade_micro < 1_000_000:
                return None, "kamino_size_too_small", available
            sized_draft = {
                **opportunity,
                "size_usdc_micro": trade_micro,
                "size_usdc": trade_micro / 1_000_000.0,
                "onchain_usdc": available,
                "execution_path": "kamino_flash",
            }
            sized, block = await self._ensure_cex_sol_for_signal(sized_draft, available)
            if block:
                return None, block, available
            trigger = (
                "prefer_flash"
                if self.config.kamino_prefer_flash
                else "low_inventory"
            )
            self.logger.info(
                "KAMINO_FLASH_SIZING | trigger=%s on_chain=$%.2f flash_usdc=$%.2f",
                trigger,
                available,
                trade_micro / 1_000_000.0,
            )
            return sized, "", available

        if not self.usdc_manager.has_minimum(available):
            reason = self.usdc_manager.replenish_block_reason()
            self.logger.warning(
                "INSUFFICIENT_USDC | available=$%.2f required=$%.2f reason=%s",
                available,
                self.usdc_manager.min_usdc,
                reason,
            )
            return None, reason, available

        sized, block = await self._size_wallet_usdc(opportunity, available, signal_micro)
        if block:
            return None, block, available
        return sized, "", available

    _KAMINO_FALLBACK_STATUSES = frozenset(
        {
            "kamino_build_failed",
            "kamino_sim_failed",
            "kamino_bundle_failed",
            "kamino_flash_error",
            "signing_unavailable",
            "live_confirm_off",
        }
    )

    async def _kamino_wallet_fallback(
        self,
        signal: dict[str, Any],
        kamino_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Use wallet Jupiter when Kamino fails but on-chain USDC can fund the trade."""
        if not self.config.kamino_wallet_first:
            return None
        status = str(kamino_result.get("status") or "")
        if status not in self._KAMINO_FALLBACK_STATUSES:
            return None
        available = float(signal.get("onchain_usdc") or 0)
        if available <= 0:
            available = await self.usdc_manager.get_available_usdc()
        min_trade = float(self.usdc_manager.min_trade_usdc)
        if available < min_trade:
            return None
        self.logger.warning(
            "KAMINO_FALLBACK_WALLET | kamino_status=%s on_chain=$%.2f min_trade=$%.2f",
            status,
            available,
            min_trade,
        )
        wallet_signal = {**signal, "execution_path": "wallet_usdc"}
        return await self.execute_basic_jupiter(wallet_signal)

    async def execute_with_kamino_flash(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Kamino borrow → Jupiter USDC→SOL → repay → Backpack sell."""
        from src.v2.kamino_flash import execute_v2_kamino_flash

        self.logger.info("EXECUTION_PATH=kamino_flash")
        try:
            result = await execute_v2_kamino_flash(self.reverse, signal, self.config)
        except Exception as exc:
            self.logger.error("KAMINO_FLASH_EXCEPTION | %s", exc, exc_info=True)
            result = {
                "status": "kamino_flash_error",
                "live_fill": False,
                "error": str(exc),
                "jupiter_step": "kamino_exception",
                "jupiter_error": str(exc),
            }
        fallback = await self._kamino_wallet_fallback(signal, result)
        if fallback is not None:
            fallback["kamino_fallback_from"] = str(result.get("status") or "")
            return fallback
        result.setdefault("execution_path", "kamino_flash")
        result.setdefault("live_fill", False)
        if not result.get("status"):
            result["status"] = "kamino_flash_error"
        return result

    @property
    def jupiter(self):
        """Jupiter executor wired through the reverse strategy."""
        return self.reverse.jupiter

    @property
    def jupiter_client(self):
        """Alias for Jupiter client (sketch-compatible name)."""
        return self.reverse.jupiter

    def _get_slippage_bps(self, size_usdc: float | None = None) -> int:
        """Dynamic slippage from cost model, floored by config execution cap."""
        usdc = float(size_usdc if size_usdc is not None else self.config.max_trade_usdc)
        modeled = self.cost_model.get_execution_slippage_bps(usdc)
        return max(modeled, int(self.config.execution_slippage_bps))

    @staticmethod
    def _map_ledger_error(error_msg: str) -> str:
        """Map Ledger bridge / device errors to actionable codes for logs + JSONL."""
        msg = str(error_msg or "")
        if "0x6a81" in msg:
            return "LEDGER_BLIND_SIGNING_REQUIRED_OR_REJECTED"
        if "UNKNOWN_ERROR" in msg:
            return "LEDGER_DEVICE_ERROR_CHECK_APP_BLIND_SIGNING"
        low = msg.lower()
        if "ledger_pubkey_mismatch" in low or "pubkey mismatch" in low:
            return "LEDGER_PUBKEY_MISMATCH"
        if "econnrefused" in low or "ledger_bridge_unreachable" in low:
            return "LEDGER_BRIDGE_DOWN_START_LEDGER_BRIDGE"
        return msg

    @staticmethod
    def _ledger_blind_signing_warning_enabled() -> bool:
        raw = (os.getenv("LEDGER_BLIND_SIGNING_WARNING") or "").strip().lower()
        return raw in ("1", "true", "yes", "on")

    def _jupiter_failure(
        self,
        error: str,
        *,
        step: str,
        buy_result: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        raw_err = str(error or step or "unknown")
        err = self._map_ledger_error(raw_err)
        detail: dict[str, Any] = {"step": step, "error": err}
        if raw_err != err:
            detail["raw_error"] = raw_err
        if buy_result:
            for key in (
                "error",
                "raw_error",
                "success",
                "txid",
                "tx_sig",
                "bundle_id",
                "tip_lamports",
            ):
                if key in buy_result:
                    detail[key] = buy_result[key]
        self.logger.error(
            "JUPITER_BUY_FAILED | step=%s jupiter_error=%s detail=%s",
            step,
            err,
            detail,
        )
        return {
            "status": "jupiter_buy_failed",
            "live_fill": False,
            "block_reason": "jupiter_buy_failed",
            "jupiter_error": err,
            "jupiter_step": step,
            "jupiter_detail": detail,
            "error": err,
            "execution_path": "wallet_usdc",
            **extra,
        }

    def _log_jupiter_failure(
        self,
        step: str,
        details: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Rich failure payload for logs + ``v2_attempts.jsonl``."""
        payload = dict(details or {})
        raw = str(payload.get("raw_error") or payload.get("error") or step)
        err = self._map_ledger_error(raw)
        if raw != err:
            payload["raw_error"] = raw
        payload["error"] = err
        return self._jupiter_failure(err, step=step, buy_result=payload, **extra)

    async def _preflight_checks(
        self,
        signal: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Ledger bridge, safety, signing, and on-chain USDC minimum."""
        ok, fail = await self._preflight_jupiter_execution()
        if not ok:
            return False, fail

        available = await self.usdc_manager.get_available_usdc()
        if not self.usdc_manager.has_minimum(available):
            self.logger.warning(
                "insufficient_usdc preflight | available=$%.2f min=$%.2f — "
                "attempting Backpack withdraw / SOL swap replenish",
                available,
                self.usdc_manager.min_usdc,
            )
            available, replenish_note = await self.usdc_manager.replenish_usdc_for_trade(
                self.reverse.backpack,
                self.reverse.jupiter,
                wallet_pubkey=self.reverse.wallet_pubkey,
            )
            if replenish_note:
                self.logger.info("USDC preflight replenish note: %s", replenish_note)

        if not self.usdc_manager.has_minimum(available):
            prepared = float((signal or {}).get("onchain_usdc") or 0)
            if available <= 0 and prepared >= self.usdc_manager.min_usdc:
                self.logger.warning(
                    "USDC RPC read $0.00 — trusting prepare_execution balance $%.2f",
                    prepared,
                )
                available = prepared

        if not self.usdc_manager.has_minimum(available):
            return False, {
                "status": "insufficient_usdc",
                "jupiter_step": "preflight_usdc_balance",
                "jupiter_error": (
                    f"available=${available:.2f} min=${self.usdc_manager.min_usdc:.2f}"
                ),
                "onchain_usdc": available,
            }

        required_sol = self._required_cex_sol_from_signal(signal or {})
        sol_ok = await self.inventory.ensure_cex_sol(
            required_sol,
            self.reverse.backpack,
            wallet_pubkey=self.reverse.wallet_pubkey,
            **self._ensure_cex_sol_context(signal or {}),
        )
        if not sol_ok:
            cex_sol = await self.inventory.get_backpack_sol(self.reverse.backpack)
            return False, {
                "status": "insufficient_backpack_sol",
                "jupiter_step": "preflight_cex_sol",
                "jupiter_error": (
                    f"backpack_sol={cex_sol:.6f} required={float(required_sol):.6f}"
                ),
                "backpack_sol": cex_sol,
                "required_cex_sol": float(required_sol),
            }
        return True, None

    async def _build_jupiter_swap(
        self,
        amount_micro: int,
        *,
        slippage_bps: int,
    ) -> dict[str, Any]:
        """Fresh quote + swap transaction (wrapAndUnwrapSol via ``build_swap_request_body``)."""
        settings = self.reverse.settings
        if settings.simulate or settings.test_mode:
            return {
                "success": True,
                "simulate": True,
                "swap_data": {"swapTransaction": "simulated"},
                "out_lamports": 0,
            }

        quote = await self.jupiter.get_quote_with_retry(
            amount_micro,
            input_mint=USDC_MINT,
            output_mint=SOL_MINT,
            slippage_bps=slippage_bps,
        )
        if not quote:
            return {
                "success": False,
                "error": "quote_failed",
                "step": "build_swap_failed",
            }

        wallet = (
            settings.wallet_pubkey
            or settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        if not wallet:
            return {
                "success": False,
                "error": "wallet_pubkey_missing",
                "step": "build_swap_failed",
            }

        swap_data = await self.jupiter.build_swap_transaction(
            {"quote": quote},
            str(wallet),
            slippage_bps=slippage_bps,
        )
        if not swap_data or "swapTransaction" not in swap_data:
            return {
                "success": False,
                "error": "swap_tx_missing",
                "step": "build_swap_failed",
            }

        return {
            "success": True,
            "quote": quote,
            "swap_data": swap_data,
            "out_lamports": int(quote.get("outAmount") or 0),
        }

    async def _sign_transaction(
        self,
        swap_result: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Ledger / hot-key sign of Jupiter swap transaction (base64)."""
        if swap_result.get("simulate"):
            return "simulated_tx", None

        swap_data = swap_result.get("swap_data") or {}
        tx_b64 = swap_data.get("swapTransaction")
        if not tx_b64:
            return None, {
                "error": "no_transaction_data",
                "step": "sign_failed",
            }

        if not await self.jupiter.has_signing():
            return None, {
                "error": "signing_unavailable",
                "step": "sign_failed",
            }

        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(str(tx_b64)))
            signed = await self.jupiter._sign_versioned(tx)
            return base64.b64encode(bytes(signed)).decode(), None
        except Exception as exc:
            raw = str(exc)
            mapped = self._map_ledger_error(raw)
            if self._ledger_blind_signing_warning_enabled() and (
                "0x6a81" in raw or mapped.startswith("LEDGER_")
            ):
                self.logger.warning(
                    "LEDGER_SIGN_HINT | %s | Solana app open, unlocked, "
                    "blind signing ON, approve swap on device",
                    mapped,
                )
            return None, {
                "error": mapped,
                "step": "sign_failed",
                "raw_error": raw[:500],
            }

    def _resolve_execution_send_path(
        self,
        gross_bps: float,
        *,
        strong_signal: bool = False,
    ) -> str:
        """Jito for high-gross / STRONG signals; RPC for marginal spreads."""
        threshold = float(os.getenv("V2_JITO_GROSS_BPS_THRESHOLD", "10"))
        if strong_signal or float(gross_bps) > threshold:
            return "jito"
        return "rpc"

    async def _send_with_jito(
        self,
        signed_b64: str,
        *,
        amount_micro: int,
        net_bps: float,
        gross_bps: float,
        strong_signal: bool = False,
    ) -> dict[str, Any]:
        """Submit signed swap via Jito (high gross) or RPC (low gross)."""
        if signed_b64 == "simulated_tx":
            return {"success": True, "txid": "simulated_tx", "tx_sig": "simulated_tx"}

        send_path = self._resolve_execution_send_path(
            gross_bps,
            strong_signal=strong_signal,
        )
        self.logger.info(
            "EXECUTION_SEND_PATH=%s gross_bps=%.2f strong=%s",
            send_path,
            gross_bps,
            strong_signal,
        )

        if send_path == "rpc":
            from src.dex.jupiter import _send_signed_rpc

            tx_result = await _send_signed_rpc(signed_b64)
            if tx_result.get("success"):
                tx_result.setdefault("send_path", "rpc")
                tx_result.setdefault("tip_lamports", 0)
                return tx_result
            err = str(tx_result.get("error") or "rpc_send_failed")
            return {
                "success": False,
                "error": err,
                "step": str(tx_result.get("step") or "rpc_send_failed"),
                "send_path": "rpc",
                "tip_lamports": 0,
                **tx_result,
            }

        from src.core.jito_tip import resolve_v2_execution_jito_tip
        from src.dex.jupiter import send_signed_swap_transaction

        tip = await resolve_v2_execution_jito_tip(
            net_bps,
            amount_micro,
            gross_bps=gross_bps,
            strong_signal=strong_signal,
        )
        if strong_signal:
            self.logger.info(
                "STRONG_JITO_PRIORITY | tip_lamports=%s dynamic=True",
                tip,
            )

        rpc_fallback_prev = os.getenv("JITO_RPC_FALLBACK_ON_FAIL")
        if strong_signal and os.getenv("V2_STRONG_PREFER_JITO", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        ):
            os.environ["JITO_RPC_FALLBACK_ON_FAIL"] = "false"

        profit_usdc = (float(net_bps) / 10_000.0) * (int(amount_micro) / 1_000_000.0)
        try:
            from src.execution.jito import send_jito_bundle_with_retry

            tx_result = await send_jito_bundle_with_retry(
                [signed_b64],
                tip_lamports=tip,
                profit_usdc=profit_usdc,
            )
            if tx_result.get("success"):
                tx_result.setdefault("tip_lamports", tip)
                tx_result.setdefault("send_path", tx_result.get("send_path") or "jito")
                tx_result.setdefault("tx_sig", tx_result.get("txid") or tx_result.get("bundle_id"))
                return tx_result

            err = str(tx_result.get("error") or "jito_send_failed")
            rpc_fallback = os.getenv("V2_STRONG_JITO_RPC_FALLBACK", "true").lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if strong_signal and rpc_fallback and any(
                tok in err.lower() for tok in ("400", "429", "bad request", "rate")
            ):
                self.logger.warning(
                    "JITO_FALLBACK_RPC | strong=%s err=%s",
                    strong_signal,
                    err[:120],
                )
                from src.dex.jupiter import _send_signed_rpc

                tx_result = await _send_signed_rpc(signed_b64)
                if tx_result.get("success"):
                    tx_result.setdefault("send_path", "rpc_fallback")
                    tx_result.setdefault("tip_lamports", 0)
                    return tx_result

            return {
                "success": False,
                "error": err,
                "step": "jito_send_failed",
                "send_path": "jito",
                "tip_lamports": tip,
                **tx_result,
            }
        except Exception as exc:
            self.logger.warning("JITO_FAILED | %s", exc, exc_info=True)
            if strong_signal and os.getenv("V2_STRONG_JITO_RPC_FALLBACK", "true").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                from src.dex.jupiter import _send_signed_rpc

                tx_result = await _send_signed_rpc(signed_b64)
                if tx_result.get("success"):
                    tx_result.setdefault("send_path", "rpc_fallback")
                    return tx_result
            return {
                "success": False,
                "error": str(exc),
                "step": "jito_send_failed",
                "send_path": "jito",
                "tip_lamports": tip,
            }
        finally:
            if strong_signal:
                if rpc_fallback_prev is None:
                    os.environ.pop("JITO_RPC_FALLBACK_ON_FAIL", None)
                else:
                    os.environ["JITO_RPC_FALLBACK_ON_FAIL"] = rpc_fallback_prev

    async def _preflight_jupiter_execution(self) -> tuple[bool, dict[str, Any] | None]:
        """Signing, live confirm, and Ledger bridge reachability before swap."""
        jupiter = self.reverse.jupiter
        settings = self.reverse.settings

        if not check_global_safety() or circuit_breaker.should_pause():
            return False, {
                "status": "safety_blocked",
                "jupiter_step": "preflight_safety",
            }

        if not self.reverse.risk.can_trade(0):
            return False, {
                "status": "risk_blocked",
                "jupiter_step": "preflight_risk",
            }

        if settings.test_mode or settings.simulate:
            return True, None

        if not settings.live_trading_confirm_enabled:
            return False, {
                "status": "live_confirm_off",
                "jupiter_step": "preflight_live_confirm",
            }

        if jupiter.quote_only:
            return False, {
                "status": "signing_unavailable",
                "jupiter_step": "preflight_quote_only",
                "jupiter_error": "no_keypair_and_no_signer_url",
            }

        if jupiter._keypair is not None:
            return True, None
        return False, {
            "status": "signing_unavailable",
            "jupiter_step": "preflight_hot_keypair",
            "jupiter_error": "hot_signer_no_keypair_loaded",
        }

    def _persist_pnl_record(
        self,
        *,
        net: Decimal,
        usdc_spent: Decimal,
        usdc_received: Decimal,
        jito_tip: Decimal,
        fees: dict[str, Any],
        total_cost: Decimal,
        signal: dict[str, Any],
        tx_sig: str,
    ) -> None:
        """Append one row to ``logs/v2_pnl.jsonl`` for monitoring / analyze_pnl.py."""
        trade_usdc = signal.get("trade_usdc") or signal.get("size_usdc") or usdc_spent
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "net_usdc": float(net),
            "usdc_spent": float(usdc_spent),
            "usdc_received": float(usdc_received),
            "jito_tip_usdc": float(jito_tip),
            "fees_jupiter": float(fees.get("jupiter", 0)),
            "fees_cex": float(fees.get("cex", 0)),
            "total_cost_usdc": float(total_cost),
            "gross_bps": float(signal.get("gross_bps") or 0),
            "net_bps": float(signal.get("net_bps") or 0),
            "trade_usdc": float(trade_usdc),
            "tx_sig": tx_sig,
            "strategy": "dex_cex_reverse",
        }
        try:
            log_path = _pnl_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            self.logger.warning("v2_pnl.jsonl write failed: %s", exc)

    def calculate_net_pnl(
        self,
        usdc_spent: Decimal,
        usdc_received: Decimal,
        jito_tip: Decimal,
        fees: dict[str, Any],
        *,
        signal: dict[str, Any],
        tx_sig: str,
    ) -> Decimal:
        """
        True net profit: CEX USDC in minus Jupiter USDC out, Jito tip, and venue fees.

        Persists each fill to ``logs/v2_pnl.jsonl``.
        """
        spent = Decimal(str(usdc_spent))
        received = Decimal(str(usdc_received))
        tip = Decimal(str(jito_tip))
        jupiter_fee = Decimal(str(fees.get("jupiter", 0)))
        cex_fee = Decimal(str(fees.get("cex", 0)))
        total_cost = spent + tip + jupiter_fee + cex_fee
        net = received - total_cost
        self._persist_pnl_record(
            net=net,
            usdc_spent=spent,
            usdc_received=received,
            jito_tip=tip,
            fees=fees,
            total_cost=total_cost,
            signal=signal,
            tx_sig=tx_sig,
        )
        return net

    def _execution_fee_breakdown(
        self,
        signal: dict[str, Any],
        buy_result: dict[str, Any],
        usdc_spent: Decimal,
    ) -> tuple[Decimal, dict[str, Decimal]]:
        """Jito tip (USDC) + estimated Jupiter / CEX fee components."""
        cex_bid = Decimal(
            str(signal.get("cex_bid") or os.getenv("V2_SOL_REPLENISH_PRICE_FALLBACK", "65"))
        )
        if cex_bid <= 0:
            cex_bid = Decimal("65")

        tip_lamports = int(buy_result.get("tip_lamports") or 0)
        jito_tip = Decimal(tip_lamports) / Decimal(1_000_000_000) * cex_bid

        base_tx_sol = Decimal(os.getenv("V2_BASE_TX_FEE_SOL", "0.00001"))
        jupiter_platform_bps = Decimal(os.getenv("V2_JUPITER_PLATFORM_FEE_BPS", "0"))
        jupiter_route = usdc_spent * jupiter_platform_bps / Decimal(10000)
        jupiter_fee = base_tx_sol * cex_bid + jupiter_route

        cex_fee_bps = Decimal(os.getenv("V2_CEX_FEE_BPS", "8"))
        cex_fee = usdc_spent * cex_fee_bps / Decimal(10000)

        return jito_tip, {"jupiter": jupiter_fee, "cex": cex_fee}

    def _required_cex_sol_from_signal(self, signal: dict[str, Any]) -> Decimal:
        """SOL needed on Backpack for CEX leg (trade size / bid + buffer)."""
        size_micro = int(signal.get("size_usdc_micro") or 0)
        trade_usdc = Decimal(
            str(
                signal.get("size_usdc")
                or signal.get("trade_usdc")
                or (size_micro / 1_000_000.0)
                or self.config.max_trade_usdc
            )
        )
        cex_bid = Decimal(str(signal.get("cex_bid") or 0))
        if cex_bid <= 0:
            cex_bid = Decimal(os.getenv("V2_SOL_REPLENISH_PRICE_FALLBACK", "65"))
        buffer = Decimal(os.getenv("V2_CEX_SOL_PREFLIGHT_BUFFER", "1.02"))
        return (trade_usdc / cex_bid) * buffer

    async def _inventory_preflight(self, signal: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """Ensure Backpack SOL inventory before Jupiter buy."""
        required_sol = self._required_cex_sol_from_signal(signal)
        ok = await self.inventory.ensure_cex_sol(
            required_sol,
            self.reverse.backpack,
            wallet_pubkey=self.reverse.wallet_pubkey,
            **self._ensure_cex_sol_context(signal),
        )
        if ok:
            return True, None
        cex_sol = await self.inventory.get_backpack_sol(self.reverse.backpack)
        self.logger.error(
            "Inventory replenish failed - skipping trade | backpack_sol=%.6f required=%.6f",
            cex_sol,
            float(required_sol),
        )
        return False, {
            "status": "inventory_replenish_failed",
            "live_fill": False,
            "block_reason": "inventory_replenish_failed",
            "jupiter_step": "preflight_inventory",
            "jupiter_error": (
                f"backpack_sol={cex_sol:.6f} required={float(required_sol):.6f}"
            ),
            "backpack_sol": cex_sol,
            "required_cex_sol": float(required_sol),
            "execution_path": "wallet_usdc",
        }

    async def _jupiter_buy_leg(
        self,
        signal: dict[str, Any],
        *,
        size_micro: int,
        size_usdc: float,
        slippage_bps: int,
        net_bps: float,
        gross_bps: float,
    ) -> dict[str, Any]:
        """Jupiter USDC→SOL: quote → sign → send."""
        self.logger.info(
            "JUPITER_BUY_START | size_usdc=%.2f size_micro=%s slippage_bps=%s",
            size_usdc,
            size_micro,
            slippage_bps,
        )

        swap_built = await self._build_jupiter_swap(
            size_micro,
            slippage_bps=slippage_bps,
        )
        if not swap_built.get("success"):
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return self._log_jupiter_failure(
                str(swap_built.get("step") or "build_swap_failed"),
                swap_built,
                slippage_bps=slippage_bps,
            )

        signed_b64, sign_fail = await self._sign_transaction(swap_built)
        if not signed_b64:
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return self._log_jupiter_failure(
                str((sign_fail or {}).get("step") or "sign_failed"),
                sign_fail or {},
            )

        send_result = await self._send_with_jito(
            signed_b64,
            amount_micro=size_micro,
            net_bps=net_bps,
            gross_bps=gross_bps,
            strong_signal=bool(
                signal.get("strong_signal") or signal.get("roundtrip_passed")
            ),
        )
        if not send_result.get("success"):
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return self._log_jupiter_failure(
                str(send_result.get("step") or "jito_send_failed"),
                send_result,
                send_path=send_result.get("send_path"),
            )

        out_lamports = int(swap_built.get("out_lamports") or 0)
        sol_received = out_lamports / 1_000_000_000.0 if out_lamports > 0 else 0.0
        tx_sig = str(
            send_result.get("tx_sig")
            or send_result.get("txid")
            or send_result.get("bundle_id")
            or ""
        )
        self.logger.info(
            "JUPITER_SWAP_SUCCESS | tx_sig=%s sol_received=%.6f usdc_spent=%.4f",
            tx_sig,
            sol_received,
            size_usdc,
        )
        return {
            "success": True,
            "tx_sig": tx_sig,
            "txid": tx_sig,
            "sol_received": sol_received,
            "usdc_spent": size_usdc,
            "amount_lamports": out_lamports,
            "tip_lamports": send_result.get("tip_lamports"),
            "send_path": send_result.get("send_path"),
            **send_result,
        }

    def _log_live_fill_pnl(
        self,
        signal: dict[str, Any],
        *,
        tx_sig: str,
        usdc_spent: Decimal,
        usdc_received: Decimal,
        jito_tip: Decimal,
        fees: dict[str, Decimal],
        net_profit: Decimal,
        sol_sold: float | None,
        gross_bps: float,
        net_bps: float,
    ) -> None:
        """Structured live-fill log with accurate net P&L."""
        net_f = float(net_profit)
        self.logger.info(
            "LIVE FILL | net_profit_usdc=%.4f usdc_spent=%.4f usdc_received=%.4f "
            "jito_tip=%.4f fees_jupiter=%.4f fees_cex=%.4f sol_sold=%s tx=%s",
            net_f,
            float(usdc_spent),
            float(usdc_received),
            float(jito_tip),
            float(fees.get("jupiter", 0)),
            float(fees.get("cex", 0)),
            sol_sold,
            tx_sig,
        )
        try:
            from src.execution.trade_logger import log_execution_trade

            log_execution_trade(
                pair=str(signal.get("pair_label") or "SOL/USDC"),
                gross_bps=gross_bps,
                net_bps=net_bps,
                size_usdc=float(usdc_spent),
                success=True,
                realized_usdc=net_f,
                tx_sig=tx_sig,
                strategy="dex_cex_reverse",
                extra={
                    "usdc_spent_jupiter": float(usdc_spent),
                    "usdc_received_cex": float(usdc_received),
                    "jito_tip_usdc": float(jito_tip),
                    "fees_jupiter": float(fees.get("jupiter", 0)),
                    "fees_cex": float(fees.get("cex", 0)),
                    "net_profit_usdc": net_f,
                    "sol_sold": sol_sold,
                },
            )
        except Exception as exc:
            self.logger.debug("live fill trade log skipped: %s", exc)

    async def execute_reverse_arb(self, signal: dict[str, Any]) -> dict[str, Any]:
        """
        Inventory preflight → Jupiter buy → Backpack sell → orphan recovery → net P&L.
        """
        self.logger.info("EXECUTION_PATH=wallet_usdc")

        try:
            size_micro = int(signal.get("size_usdc_micro") or 0)
            if size_micro <= 0:
                size_usdc = float(
                    signal.get("size_usdc") or self.config.max_trade_usdc
                )
                size_micro = int(size_usdc * 1_000_000)
            size_usdc = size_micro / 1_000_000.0
            slippage_bps = self._get_slippage_bps(size_usdc)
            net_bps = float(signal.get("net_bps") or 0)
            gross_bps = float(signal.get("gross_bps") or 0)

            self.logger.info("EXECUTING_REVERSE_ARB | size_usdc=%.2f", size_usdc)

            ok, preflight_fail = await self._preflight_checks(signal)
            if not ok and preflight_fail:
                status = str(preflight_fail.get("status") or "preflight_failed")
                self.logger.error(
                    "JUPITER_PREFLIGHT_FAILED | status=%s step=%s error=%s",
                    status,
                    preflight_fail.get("jupiter_step"),
                    preflight_fail.get("jupiter_error"),
                )
                return {
                    "live_fill": False,
                    "block_reason": status,
                    "execution_path": "wallet_usdc",
                    **preflight_fail,
                }

            if size_micro < 1_000_000:
                return self._log_jupiter_failure(
                    "trade_size_too_small",
                    {"error": "trade_size_too_small", "size_usdc_micro": size_micro},
                )

            if not self.reverse.risk.can_trade(size_micro):
                return {
                    "status": "risk_size_blocked",
                    "live_fill": False,
                    "jupiter_step": "preflight_risk_size",
                    "execution_path": "wallet_usdc",
                }

            buy_result = await self._jupiter_buy_leg(
                signal,
                size_micro=size_micro,
                size_usdc=size_usdc,
                slippage_bps=slippage_bps,
                net_bps=net_bps,
                gross_bps=gross_bps,
            )
            if not buy_result.get("success"):
                return buy_result

            tx_sig = str(buy_result.get("tx_sig") or "")
            sol_received = float(buy_result.get("sol_received") or 0)

            sell_signal = {**signal, "v2_handles_pnl": True}
            sell_result = await self.reverse.complete_cex_sell_after_buy(
                sell_signal,
                size_usdc_micro=size_micro,
                buy_result=buy_result,
                gross_bps=gross_bps,
            )

            if sell_result.get("live_fill"):
                usdc_spent = Decimal(str(size_usdc))
                usdc_received = Decimal(
                    str(
                        sell_result.get("usdc_received")
                        or sell_result.get("realized_usdc")
                        or 0
                    )
                )
                jito_tip, fees = self._execution_fee_breakdown(
                    signal, buy_result, usdc_spent
                )
                net_profit = self.calculate_net_pnl(
                    usdc_spent,
                    usdc_received,
                    jito_tip,
                    fees,
                    signal=signal,
                    tx_sig=tx_sig,
                )
                net_f = float(net_profit)
                fees_total = float(jito_tip + fees["jupiter"] + fees["cex"])

                sell_result.setdefault("jupiter_step", "complete")
                sell_result["jupiter_tx_sig"] = tx_sig
                sell_result["block_reason"] = "filled"
                sell_result["usdc_spent_jupiter"] = float(usdc_spent)
                sell_result["usdc_received_cex"] = float(usdc_received)
                sell_result["jito_tip_usdc"] = float(jito_tip)
                sell_result["fees_jupiter"] = float(fees["jupiter"])
                sell_result["fees_cex"] = float(fees["cex"])
                sell_result["fees_usdc"] = fees_total
                sell_result["net_profit_usdc"] = net_f
                sell_result["realized_usdc"] = net_f
                if buy_result.get("send_path"):
                    sell_result["send_path"] = buy_result["send_path"]

                record_trade_execution("dex_cex_reverse", success=True, pnl_usd=net_f)
                self.reverse.risk.record_trade_result(net_f)
                self._log_live_fill_pnl(
                    signal,
                    tx_sig=tx_sig,
                    usdc_spent=usdc_spent,
                    usdc_received=usdc_received,
                    jito_tip=jito_tip,
                    fees=fees,
                    net_profit=net_profit,
                    sol_sold=float(sell_result.get("sol_sold") or 0) or None,
                    gross_bps=gross_bps,
                    net_bps=net_bps,
                )
                return sell_result

            if str(sell_result.get("status") or "").startswith("jupiter"):
                return self._log_jupiter_failure(
                    str(sell_result.get("error") or sell_result.get("status")),
                    sell_result,
                    tx_sig=tx_sig,
                )

            if str(sell_result.get("status") or "") == "cex_sell_failed":
                sell_result.setdefault("jupiter_step", "cex_sell_failed")
                sell_result["jupiter_tx_sig"] = tx_sig
                sell_result["block_reason"] = "cex_sell_failed"
                if buy_result.get("send_path"):
                    sell_result["send_path"] = buy_result["send_path"]
                recovery = await self.inventory.recover_orphan(
                    tx_sig,
                    sol_received,
                    self.reverse.backpack,
                    wallet_pubkey=self.reverse.wallet_pubkey,
                    jupiter=self.reverse.jupiter,
                )
                sell_result["orphan_recovery"] = recovery
                if recovery.get("success"):
                    self.logger.info(
                        "ORPHAN_RECOVERED | path=%s tx=%s",
                        recovery.get("recovery_path"),
                        recovery.get("tx_sig") or tx_sig,
                    )
                else:
                    self.logger.warning(
                        "ORPHAN_RECOVERY_FAILED | tx=%s err=%s",
                        tx_sig,
                        recovery.get("error"),
                    )
            return sell_result

        except Exception as exc:
            self.logger.error("EXECUTION_EXCEPTION | %s", exc, exc_info=True)
            record_trade_execution("dex_cex_reverse", success=False, pnl_usd=0.0)
            return {
                "status": "execution_exception",
                "live_fill": False,
                "block_reason": "execution_exception",
                "jupiter_step": "exception",
                "jupiter_error": str(exc),
                "error": str(exc),
                "execution_path": "wallet_usdc",
            }

    async def execute_dex_cex_reverse(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Reverse arb entry → ``execute_reverse_arb``."""
        try:
            return await self.execute_reverse_arb(signal)
        except Exception as exc:
            self.logger.error("Execution failed: %s", exc, exc_info=True)
            return {
                "status": "execution_failed",
                "live_fill": False,
                "block_reason": "execution_failed",
                "jupiter_error": str(exc),
                "execution_path": "wallet_usdc",
            }

    async def execute_basic_jupiter(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Alias for ``execute_reverse_arb`` (wallet USDC path)."""
        return await self.execute_reverse_arb(signal)

    async def execute(self, signal: dict[str, Any]) -> dict[str, Any]:
        """Route by ``execution_path`` from sizing (wallet-first when funded)."""
        # === INVENTORY MANAGEMENT (before Jupiter / Kamino buy) ===
        inv_ok, inv_fail = await self._inventory_preflight(signal)
        if not inv_ok and inv_fail:
            self.logger.error("Inventory replenish failed - skipping trade")
            return inv_fail

        path = str(signal.get("execution_path") or "wallet_usdc")
        if path == "kamino_flash" and self.config.enable_kamino_flash:
            return await self.execute_with_kamino_flash(signal)
        return await self.execute_dex_cex_reverse(signal)
