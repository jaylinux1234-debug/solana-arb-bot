# src/strategies/cex_dex.py
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from solana.rpc.async_api import AsyncClient
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

import src.core.wallet as wallet_safety
from src.cex.executor import CexExecutor, cex_executor
from src.cex.inventory import CexInventoryTracker
from src.cex.price_feed import cex_feed
from src.config.settings import settings
from src.core.circuit_breaker import circuit_breaker
from src.core.wallet_safety import check_global_safety
from src.dex.jupiter import JupiterClient, JupiterExecutor
from src.dex.kamino import Kamino, KaminoFlashLoan
from src.dex.quote import get_jupiter_quote
from src.execution.jito import JitoMultiRelay, configure_jito, send_jito_bundle_multi_relay
from src.strategies.brain_signals import note_cex_dex_context
from src.strategies.cex_dex_core import (
    Direction,
    analyze_cex_dex_spread,
    cex_dex_ai_min_confidence,
    clamp_trade_usdc_micro,
    load_cex_dex_cost_params,
    net_spread_bps_after_costs,
    resolve_direction,
)
from src.strategies.cex_dex_inventory import inventory_cap_blocks
from src.utils.ai import ai_agent_decide, enhanced_ai_approve

logger = logging.getLogger(__name__)

USDC_MINT_STR = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT_STR = "So11111111111111111111111111111111111111112"


def _test_mode() -> bool:
    return settings.test_mode


def _gross_spread_buffer_bps() -> int:
    return int(os.getenv("CEX_DEX_GROSS_BUFFER_BPS", "10"))


def _strong_gross_spread_buffer_bps() -> int:
    return int(os.getenv("CEX_DEX_STRONG_GROSS_BUFFER_BPS", "60"))


# wallet_safety naming alias
check_safety = wallet_safety.check_global_safety


async def simulate(client: AsyncClient, tx: VersionedTransaction) -> bool:
    sim = await client.simulate_transaction(tx)
    if sim.value.err is None:
        return True
    logger.warning("CEX-DEX flash simulation failed: %s", sim.value.err)
    return False


async def build_versioned_tx(
    jupiter: JupiterClient,
    instructions: list[Instruction],
    *,
    alt_addresses: list[str] | None = None,
) -> VersionedTransaction:
    return await jupiter.build_flash_loan_tx(
        instructions,
        alt_address_strings=alt_addresses or [],
    )


def _jupiter_swap_instructions(
    kamino: KaminoFlashLoan,
    payload: dict,
) -> list[Instruction]:
    if payload.get("error"):
        raise ValueError(f"Jupiter swap-instructions error: {payload.get('error')}")
    if not payload.get("swapInstruction"):
        raise ValueError("Jupiter swap-instructions missing swapInstruction")
    out: list[Instruction] = []
    for cb in payload.get("computeBudgetInstructions") or []:
        out.append(kamino._convert_jupiter_ix(cb))
    for setup in payload.get("setupInstructions") or []:
        out.append(kamino._convert_jupiter_ix(setup))
    out.append(kamino._convert_jupiter_ix(payload["swapInstruction"]))
    if cleanup := payload.get("cleanupInstruction"):
        out.append(kamino._convert_jupiter_ix(cleanup))
    return out


async def build_cex_dex_flash_tx(
    cex_mid: float,
    dex_mid: float,
    size_usdc: int,
    *,
    client: AsyncClient,
    keypair: Keypair | None,
    jupiter: JupiterClient | None = None,
    direction: str | None = None,
    slippage_bps: int | None = None,
) -> VersionedTransaction | None:
    """Kamino flash borrow → Jupiter swap → flash repay (sign via keypair or Ledger on jupiter)."""
    direction = resolve_direction(direction, cex_mid, dex_mid) or (
        "dex_cheap" if dex_mid > cex_mid else "cex_cheap"
    )
    if keypair is not None:
        user = keypair.pubkey()
    elif jupiter is not None:
        user = jupiter.signer_pubkey()
    else:
        raise ValueError("build_cex_dex_flash_tx requires keypair or jupiter with signer configured")
    bps = (
        slippage_bps
        if slippage_bps is not None
        else int(os.getenv("CEX_DEX_FLASH_QUOTE_SLIPPAGE_BPS", str(settings.max_slippage_bps)))
    )

    own_jupiter = jupiter is None
    if own_jupiter:
        jupiter = JupiterClient()

    kamino_helper = KaminoFlashLoan(client, keypair)

    try:
        if direction == "dex_cheap":
            borrow_mint = USDC_MINT_STR
            flash_amount = size_usdc
            quote_in, quote_out = USDC_MINT_STR, SOL_MINT_STR
        else:
            borrow_mint = SOL_MINT_STR
            flash_amount = max(1, int(size_usdc * 1_000 / cex_mid * 0.965))
            quote_in, quote_out = SOL_MINT_STR, USDC_MINT_STR

        borrow_ix = Kamino.get_flash_borrow_ix(borrow_mint, flash_amount, user)

        quote = await jupiter.get_jupiter_quote(quote_in, quote_out, flash_amount, slippage_bps=bps)
        if not quote or quote.get("error"):
            logger.error("Jupiter quote failed for CEX-DEX flash")
            return None

        payload = await jupiter.get_swap_instructions(quote, slippage_bps=bps)
        swap_ixs = _jupiter_swap_instructions(kamino_helper, payload)

        fee = kamino_helper._estimate_flash_loan_fee(flash_amount)
        repay_ix = Kamino.get_flash_repay_ix(
            borrow_mint,
            flash_amount + fee,
            user,
            borrow_ix_index=0,
        )

        alt_addrs = list(payload.get("addressLookupTableAddresses") or [])
        return await build_versioned_tx(
            jupiter,
            [borrow_ix, *swap_ixs, repay_ix],
            alt_addresses=alt_addrs,
        )
    except Exception:
        logger.exception("build_cex_dex_flash_tx failed")
        return None
    finally:
        if own_jupiter:
            await jupiter.client.close()


async def execute_cex_dex_flash(
    cex_mid: float,
    dex_mid: float,
    size_usdc: int,
    *,
    client: AsyncClient,
    keypair: Keypair,
    jupiter: JupiterClient | None = None,
    direction: str | None = None,
    slippage_bps: int | None = None,
    tip_lamports: int | None = None,
    multi_relay: bool = True,
) -> str | None:
    """
    Kamino flash borrow → Jupiter swap → flash repay → simulate → Jito bundle.

    ``size_usdc`` is USDC amount in micro-units (6 decimals).
    """
    if circuit_breaker.should_pause():
        logger.warning("CEX-DEX flash blocked by circuit breaker")
        return None

    own_jupiter = jupiter is None
    if own_jupiter:
        jupiter = JupiterClient()

    try:
        tx = await build_cex_dex_flash_tx(
            cex_mid,
            dex_mid,
            size_usdc,
            client=client,
            keypair=keypair,
            jupiter=jupiter,
            direction=direction,
            slippage_bps=slippage_bps,
        )
        if tx is None:
            return None

        sim_attempts = max(1, int(os.getenv("CEX_DEX_FLASH_SIM_ATTEMPTS", "3")))
        sim_ok = False
        for attempt in range(sim_attempts):
            if await simulate(client, tx):
                sim_ok = True
                break
            if attempt + 1 < sim_attempts:
                await asyncio.sleep(float(os.getenv("CEX_DEX_FLASH_SIM_RETRY_DELAY_SEC", "0.4")))
        if not sim_ok:
            logger.error("CEX-DEX flash simulation failed after retries")
            return None

        wallet_safety.record_successful_simulation()

        if _test_mode():
            logger.info(
                "TEST_MODE: would send CEX-DEX flash | size_usdc_micro=%s tip=%s",
                size_usdc,
                tip_lamports,
            )
            return None

        ok, reason = wallet_safety.before_live_send(size_usdc)
        if not ok:
            logger.warning("Wallet safety blocked CEX-DEX flash: %s", reason)
            return None

        tip = tip_lamports
        if tip is None:
            tip = int(os.getenv("CEX_DEX_FLASH_JITO_TIP_LAMPORTS", "120000"))

        send_fn = send_jito_bundle_multi_relay if multi_relay else None
        if send_fn:
            bundle_id = await send_fn(
                [tx],
                client=client,
                keypair=keypair,
                tip_lamports=tip,
            )
        else:
            from src.execution.jito import send_jito_bundle

            bundle_id = await send_jito_bundle(
                [tx],
                client=client,
                keypair=keypair,
                tip_lamports=tip,
            )

        if bundle_id:
            wallet_safety.record_live_trade_usdc_micro(size_usdc)
            logger.info("CEX-DEX flash sent | bundle=%s tip=%s", bundle_id, tip)
        return bundle_id

    except Exception:
        logger.exception("execute_cex_dex_flash failed")
        return None
    finally:
        if own_jupiter:
            await jupiter.client.close()


@dataclass
class CexDexOpportunity:
    direction: Direction
    cex_mid: float
    dex_mid: float
    spread_bps_net: float
    size_usdc_micro: int
    analysis: dict


class CexDexStrategy:
    """CEX ask vs Jupiter implied SOL → AI → Kamino sim → Jito multi-relay."""

    def __init__(
        self,
        client: AsyncClient | None = None,
        keypair: Keypair | None = None,
        executor: JupiterExecutor | None = None,
    ) -> None:
        if executor is not None:
            self.jupiter = executor
            self.executor = executor
            self.client = client or executor.client
            self.keypair = keypair or executor.keypair
        else:
            self.jupiter = JupiterExecutor()
            self.executor = self.jupiter
            self.client = self.jupiter.client
            self.keypair = self.jupiter.keypair

        self.cex = CexExecutor()
        self.inventory = CexInventoryTracker()
        configure_jito(self.client, self.keypair)
        self.jito = JitoMultiRelay(client=self.client, keypair=self.keypair)

        self.base_cost, self.wdraw_latency, self.depth_util, self.max_impact = (
            load_cex_dex_cost_params()
        )
        self.min_net_bps = int(
            os.getenv(
                "MIN_NET_PROFIT_BPS",
                os.getenv("CEX_DEX_MIN_NET_SPREAD_BPS", str(settings.min_net_profit_bps)),
            )
        )
        self.max_trade = int(
            os.getenv("CEX_DEX_MAX_TRADE_USDC_MICRO", str(settings.cex_dex_max_trade_usdc_micro))
        )
        self.flash_cap = int(os.getenv("CEX_DEX_FLASH_AMOUNT_USDC_MICRO", "50000000"))
        self.min_trade = int(
            os.getenv("CEX_DEX_MIN_TRADE_USDC_MICRO", str(settings.cex_dex_min_trade_usdc_micro))
        )
        self.max_inventory_sol = float(
            os.getenv(
                "CEX_DEX_MAX_INVENTORY_SOL",
                os.getenv("INVENTORY_MAX_SOL", str(settings.inventory_max_sol)),
            )
        )
        self.probe_usdc_micro = int(os.getenv("CEX_DEX_PROBE_USDC_MICRO", "50000000"))
        self._ai_min_conf = cex_dex_ai_min_confidence()
        self._cycle_ctx: dict[str, float | str] = {}

    async def run_cycle(self) -> bool:
        """Delegate to unified ``CexDexCycle`` (preferred entry)."""
        from src.strategies.cex_dex_cycle import cex_dex_cycle

        return await cex_dex_cycle.run_once()

    async def _run_cycle_legacy(self) -> bool:
        if not check_global_safety() or circuit_breaker.should_pause():
            return False

        cex_ask = await self.cex.get_best_ask("SOL_USDC")
        if not cex_ask or cex_ask <= 0:
            return False

        probe_usdc = self.probe_usdc_micro
        jup_price, quote = await self.jupiter.get_implied_usdc_per_sol(probe_usdc)
        if not jup_price or not quote:
            return False

        gross_bps = abs((cex_ask - jup_price) / cex_ask) * 10000.0
        if gross_bps < self.min_net_bps + _strong_gross_spread_buffer_bps():
            note_cex_dex_context({"active": False, "gross_bps": gross_bps})
            return False

        size_usdc = self._adaptive_size(gross_bps)
        if size_usdc < settings.cex_dex_min_trade_usdc_micro:
            return False

        cex_bid, _ = await self.cex.get_bid_ask("SOL/USDC")
        cex_bid = cex_bid if cex_bid else cex_ask
        if not await enhanced_ai_approve(
            {
                "strategy": "cex_dex_arb",
                "gross_bps": round(gross_bps, 2),
                "size_usdc": size_usdc,
                "size_usdc_micro": size_usdc,
                "cex_ask": cex_ask,
                "cex_bid": cex_bid,
                "jupiter_usdc_per_sol": jup_price,
            },
            min_confidence=self._ai_min_conf,
        ):
            return False

        cex_mid = (float(cex_bid) + float(cex_ask)) / 2.0
        direction = "cex_cheap" if jup_price > cex_ask else "dex_cheap"
        self._cycle_ctx = {
            "cex_mid": cex_mid,
            "dex_mid": float(jup_price),
            "direction": direction,
            "cex_ask": float(cex_ask),
        }

        if not await self._simulate_trade(size_usdc):
            return False

        tip = self._calc_dynamic_tip(gross_bps, size_usdc)
        return await self._execute_trade(size_usdc, tip)

    def _adaptive_size(self, gross_bps: float, price: float | None = None) -> int:
        if price is None:
            max_size = settings.cex_dex_max_trade_usdc_micro
            factor = min(1.0, float(gross_bps) / 300.0)
            return int(max_size * factor * 0.75)

        util = float(os.getenv("CEX_DEX_DEPTH_UTILIZATION", str(self.depth_util)))
        edge_scale = min(1.0, float(gross_bps) / 200.0)
        raw = int(self.max_trade * util * edge_scale)
        liq_usdc = int(50.0 * price * util * 1_000_000)
        size = clamp_trade_usdc_micro(
            max_trade_usdc_micro=self.max_trade,
            flash_cap_usdc_micro=self.flash_cap,
            liquidity_cap_usdc_micro=liq_usdc,
            min_trade_usdc_micro=self.min_trade,
        )
        if raw > 0:
            size = min(size, raw)
        return self.inventory.cap_trade_usdc_micro(size, price, self.max_inventory_sol)

    def _calc_dynamic_tip(self, gross_bps: float, size: int) -> int:
        est_profit_usdc = (size / 1_000_000) * (gross_bps / 10000.0)
        sol_px = max(1.0, float(self._cycle_ctx.get("cex_ask") or 1.0))
        tip = int(est_profit_usdc * settings.dynamic_tip_multiplier * 1_000_000_000 / sol_px)
        lo = int(os.getenv("JITO_TIP_LAMPORTS_MIN", "50000"))
        hi = int(os.getenv("MAX_TIP_LAMPORTS", str(settings.max_tip_lamports)))
        return max(lo, min(hi, tip))

    async def _simulate_trade(self, size: int) -> bool:
        ctx = self._cycle_ctx
        cex_mid = float(ctx.get("cex_mid") or 0)
        dex_mid = float(ctx.get("dex_mid") or 0)
        direction = str(ctx.get("direction") or "dex_cheap")
        logger.info("Simulating Kamino+Jupiter trade | size=%.2f USDC", size / 1e6)

        tx = await build_cex_dex_flash_tx(
            cex_mid,
            dex_mid,
            size,
            client=self.client,
            keypair=self.keypair,
            jupiter=self.jupiter,
            direction=direction,
        )
        if tx is None:
            return False
        if await simulate(self.client, tx):
            wallet_safety.record_successful_simulation()
            return True
        return False

    async def _execute_trade(self, size: int, tip: int) -> bool:
        if settings.test_mode:
            logger.info(
                "TEST_MODE: would execute %.2f USDC trade with tip %s lamports",
                size / 1e6,
                tip,
            )
            return True

        if not settings.live_trading_confirm_enabled:
            logger.warning("LIVE_TRADING_CONFIRM not set — skipping live send")
            return False

        ctx = self._cycle_ctx
        txs = await self._build_swap_bundle(
            size,
            cex_mid=float(ctx.get("cex_mid") or 0),
            dex_mid=float(ctx.get("dex_mid") or 0),
            direction=str(ctx.get("direction") or "dex_cheap"),
        )
        if not txs:
            return False

        ok, reason = wallet_safety.before_live_send(size)
        if not ok:
            logger.warning("Wallet safety blocked bundle: %s", reason)
            return False

        bundle_id = await self.jito.send_bundle(txs, int(tip), append_tip_tx=True)
        if bundle_id:
            wallet_safety.record_live_trade_usdc_micro(size)
            logger.info("CEX-DEX bundle sent | bundle=%s tip=%s", bundle_id, tip)
            return True
        return False

    async def _build_swap_bundle(
        self,
        size_usdc_micro: int,
        *,
        cex_mid: float,
        dex_mid: float,
        direction: str,
    ) -> list[VersionedTransaction]:
        tx = await build_cex_dex_flash_tx(
            cex_mid,
            dex_mid,
            size_usdc_micro,
            client=self.client,
            keypair=self.keypair,
            jupiter=self.jupiter,
            direction=direction,
        )
        return [tx] if tx is not None else []

    async def evaluate_and_execute(
        self,
        cex_bid: float,
        cex_ask: float,
        probe_usdc: int | None = None,
    ) -> str | None:
        """
        Fresh Jupiter probe → gross spread gate → adaptive size → AI → sim → multi-relay Jito.
        Returns bundle id on success, else ``None``.
        """
        if circuit_breaker.should_pause():
            return None

        await self._sync_inventory()
        current_inventory_sol = self.inventory.sol_estimate
        if inventory_cap_blocks(current_inventory_sol, self.max_inventory_sol):
            return None

        probe = probe_usdc if probe_usdc is not None else self.probe_usdc_micro
        jup_price, quote = await self.jupiter.get_implied_usdc_per_sol(probe)
        if not jup_price or not quote:
            return None

        if cex_ask <= 0:
            return None

        gross_bps = abs(cex_ask - jup_price) / cex_ask * 10000.0
        if gross_bps < self.min_net_bps + _gross_spread_buffer_bps():
            note_cex_dex_context({"active": False, "gross_bps": gross_bps})
            return None

        size_usdc = self._adaptive_size(gross_bps, cex_ask)
        if size_usdc < self.min_trade:
            return None

        _, vol_bps = await cex_feed.get_price_and_volatility_bps("SOL/USDC")
        if not await self._ai_approve(
            gross_bps,
            size_usdc,
            cex_bid=cex_bid,
            cex_ask=cex_ask,
            jup_price=jup_price,
            volatility_bps=vol_bps,
        ):
            return None

        cex_mid = (cex_bid + cex_ask) / 2.0
        direction = "cex_cheap" if jup_price > cex_ask else "dex_cheap"

        self._cycle_ctx = {
            "cex_mid": cex_mid,
            "dex_mid": float(jup_price),
            "direction": direction,
            "cex_ask": float(cex_ask),
        }
        if not await self._simulate_trade(size_usdc):
            return None

        tip = self._calc_dynamic_tip(gross_bps, size_usdc)
        return await self._execute_with_bundle(
            size_usdc,
            tip,
            cex_mid=cex_mid,
            dex_mid=jup_price,
            direction=direction,
        )

    async def _execute_with_bundle(
        self,
        size_usdc_micro: int,
        tip_lamports: int,
        *,
        cex_mid: float,
        dex_mid: float,
        direction: str,
    ) -> str | None:
        self._cycle_ctx = {
            "cex_mid": cex_mid,
            "dex_mid": dex_mid,
            "direction": direction,
            "cex_ask": cex_mid,
        }
        if await self._execute_trade(size_usdc_micro, tip_lamports):
            return "bundle_ok"
        return None

    async def _ai_approve(
        self,
        gross_bps: float,
        size_usdc_micro: int,
        *,
        cex_bid: float,
        cex_ask: float,
        jup_price: float,
        volatility_bps: float = 0.0,
    ) -> bool:
        await self._sync_inventory()
        return await enhanced_ai_approve(
            {
                "strategy": "cex_dex_arb",
                "gross_bps": gross_bps,
                "size_usdc_micro": size_usdc_micro,
                "cex_bid": cex_bid,
                "cex_ask": cex_ask,
                "jupiter_usdc_per_sol": jup_price,
                "volatility_bps": volatility_bps,
                "inventory_sol": self.inventory.sol_estimate,
                "max_inventory_sol": self.max_inventory_sol,
                "cex_prices": await cex_feed.get_multiple_prices(["SOL/USDC"]),
            },
            min_confidence=self._ai_min_conf,
        )

    async def detect(self) -> CexDexOpportunity | None:
        await self._sync_inventory()

        # Rate limit + inventory guard
        current_inventory_sol = self.inventory.sol_estimate
        if inventory_cap_blocks(current_inventory_sol, self.max_inventory_sol):
            return None

        cex_bid, cex_ask, cex_mid = await cex_feed.get_bid_ask_mid("SOL/USDC")
        if not cex_mid:
            cex_mid = await cex_feed.get_price("SOL/USDC")
            if not cex_mid:
                return None
            cex_bid = cex_ask = cex_mid

        vol_bps = 0.0
        _, vol_bps = await cex_feed.get_price_and_volatility_bps("SOL/USDC")

        dex_mid, probe_quote = await self.jupiter.get_implied_usdc_per_sol(self.probe_usdc_micro)
        if not dex_mid:
            probe_quote = await get_jupiter_quote(SOL_MINT_STR, USDC_MINT_STR, 1_000_000_000)
            if not probe_quote or "outAmount" not in probe_quote:
                return None
            dex_mid = int(probe_quote["outAmount"]) / 1_000_000

        analysis = analyze_cex_dex_spread(cex_mid, dex_mid)
        if not analysis:
            return None

        net_bps = net_spread_bps_after_costs(
            analysis.spread_bps_abs,
            int(getattr(self, "probe_usdc_micro", settings.CEX_DEX_PROBE_USDC_MICRO)),
            direction=analysis.direction,
            volatility_bps=vol_bps,
        )

        if net_bps < self.min_net_bps:
            note_cex_dex_context({"active": False, "net_bps": net_bps})
            return None

        gross_for_size = analysis.spread_bps_abs
        size = self._adaptive_size(gross_for_size, cex_mid)
        if size <= 0:
            return None

        opp = CexDexOpportunity(
            direction=analysis.direction,
            cex_mid=cex_mid,
            dex_mid=dex_mid,
            spread_bps_net=net_bps,
            size_usdc_micro=size,
            analysis=analysis.__dict__,
        )

        note_cex_dex_context(
            {
                "active": True,
                "direction": opp.direction,
                "spread_bps_net": net_bps,
                "size_usdc_micro": size,
                "cex_mid": cex_mid,
                "cex_bid": cex_bid,
                "cex_ask": cex_ask,
            }
        )
        return opp

    async def _sync_inventory(self):
        if not getattr(self, "_synced", False):
            try:
                sol = await cex_executor.get_balance("SOL")
                self.inventory.seed(sol)
                self._synced = True
            except Exception:
                pass

    async def execute(self, opp: CexDexOpportunity):
        if circuit_breaker.should_pause():
            return None

        cex_bid, cex_ask, _ = await cex_feed.get_bid_ask_mid("SOL/USDC")
        if cex_bid and cex_ask:
            bundle_id = await self.evaluate_and_execute(cex_bid, cex_ask)
            if bundle_id:
                return bundle_id

        decision = await ai_agent_decide(
            {
                "strategy": "cex_dex_arb",
                **opp.__dict__,
                "cex_prices": await cex_feed.get_multiple_prices(["SOL/USDC"]),
            },
            (await self.client.get_balance(self.keypair.pubkey())).value,
            min_confidence=self._ai_min_conf,
        )

        if decision.get("final_action") != "APPROVE":
            return None

        self._cycle_ctx = {
            "cex_mid": opp.cex_mid,
            "dex_mid": opp.dex_mid,
            "direction": opp.direction,
            "cex_ask": opp.cex_mid,
        }
        tip = self._calc_dynamic_tip(opp.spread_bps_net, opp.size_usdc_micro)
        if await self._execute_trade(opp.size_usdc_micro, tip):
            return "bundle_ok"
        return await execute_cex_dex_flash(
            opp.cex_mid,
            opp.dex_mid,
            opp.size_usdc_micro,
            client=self.client,
            keypair=self.keypair,
            jupiter=self.executor,
            direction=opp.direction,
            tip_lamports=tip,
            multi_relay=True,
        )


cex_dex_strategy = CexDexStrategy()
