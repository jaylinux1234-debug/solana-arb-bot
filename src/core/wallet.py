# src/core/wallet.py
"""
Wallet utilities for balance checking, plus safety rails: sim counters, live-send cooldown,
daily notional cap (USDC micro), equity drawdown tracking, and global safety checks.

Zenbook / ops: keep TEST_MODE=true until successful_sim_count meets MIN_SUCCESSFUL_SIMS_BEFORE_LIVE,
size collateral/backrun notionals conservatively, and avoid unnecessary show_key.py runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config.settings import get_settings, settings
from src.core import wallet_safety as _ws
from src.core.circuit_breaker import circuit_breaker
from src.utils.alerts import schedule_alert

logger = logging.getLogger(__name__)


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def get_usdc_balance() -> float:
    """Get USDC balance (Backpack for CEX-DEX)."""
    try:
        from src.cex.backpack import get_backpack_client

        client = get_backpack_client()
        return await client.get_balance("USDC")
    except Exception as exc:
        logger.warning("Failed to get USDC balance: %s", exc)
        return 0.0


def _parse_usdc_token_accounts(resp: Any) -> float:
    total = 0.0
    for entry in resp.value or []:
        parsed = entry.account.data.parsed  # type: ignore[attr-defined]
        if not isinstance(parsed, dict):
            continue
        amount = parsed.get("info", {}).get("tokenAmount", {})
        ui = amount.get("uiAmount")
        if ui is not None:
            total += float(ui)
        else:
            raw = int(amount.get("amount", 0) or 0)
            decimals = int(amount.get("decimals", 6) or 6)
            total += raw / (10**decimals)
    return total


async def _fetch_onchain_usdc_once(pubkey: str) -> float:
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.types import TokenAccountOpts
    from solders.pubkey import Pubkey

    from src.core.rpc_config import call_with_rpc_fallback
    from src.core.rpc_urls import rpc_provider_label
    from src.monitoring.metrics import record_rpc_latency

    owner = Pubkey.from_string(pubkey)
    mint = Pubkey.from_string(USDC_MINT)

    async def _fetch(rpc: str) -> float:
        t0 = time.perf_counter()
        async with AsyncClient(rpc) as client:
            resp = await client.get_token_accounts_by_owner_json_parsed(
                owner,
                TokenAccountOpts(mint=mint),
            )
        record_rpc_latency(
            rpc_provider_label(rpc),
            "get_token_accounts",
            time.perf_counter() - t0,
        )
        return _parse_usdc_token_accounts(resp)

    return float(await call_with_rpc_fallback("balance", _fetch, label="usdc_balance"))


async def _redis_get_usdc_balance(cache_key: str) -> float | None:
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(url)
        try:
            raw = await client.get(cache_key)
        finally:
            await client.aclose()
        if raw is None:
            return None
        return float(raw)
    except Exception as exc:
        logger.debug("Redis USDC cache read skipped: %s", exc)
        return None


async def _redis_set_usdc_balance(
    cache_key: str,
    balance: float,
    *,
    max_age_sec: int,
) -> None:
    url = (os.getenv("REDIS_URL") or "").strip()
    if not url or balance <= 0:
        return
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(url)
        try:
            await client.setex(cache_key, max(1, max_age_sec), str(balance))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.debug("Redis USDC cache write skipped: %s", exc)


async def get_usdc_balance_robust(
    wallet_pubkey: str | None = None,
    *,
    max_age_sec: int | None = None,
) -> float:
    """
    SPL USDC balance with cache + RPC fallback.

    Avoids false ``$0.00`` reads during Helius 429 cooldowns by reusing a
    recent successful balance when live fetches fail.
    """
    pubkey = (wallet_pubkey or get_wallet_pubkey() or "").strip()
    if not pubkey:
        return 0.0

    from src.core.rpc_config import (
        cache_balance,
        get_cached_balance,
        get_stale_cached_balance,
        is_rate_limited_error,
    )

    ttl = int(
        max_age_sec
        if max_age_sec is not None
        else float(os.getenv("USDC_BALANCE_CACHE_SEC", "8"))
    )
    stale_ttl = float(os.getenv("USDC_BALANCE_STALE_CACHE_SEC", "120"))
    cache_key = f"balance:usdc:{pubkey}"

    redis_cached = await _redis_get_usdc_balance(cache_key)
    if redis_cached is not None and redis_cached > 0:
        cache_balance(cache_key, redis_cached)
        return redis_cached

    mem_cached = get_cached_balance(cache_key, ttl)
    if mem_cached is not None:
        return mem_cached

    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            balance = await _fetch_onchain_usdc_once(pubkey)
            if balance > 0:
                cache_balance(cache_key, balance)
                await _redis_set_usdc_balance(cache_key, balance, max_age_sec=ttl)
                return balance
            last_exc = RuntimeError("zero_usdc_balance")
        except Exception as exc:
            last_exc = exc
            if is_rate_limited_error(exc):
                logger.warning(
                    "Helius/RPC 429 on USDC balance - retry %s/4",
                    attempt + 1,
                )
            else:
                logger.warning(
                    "USDC balance fetch failed (attempt %s/4): %s",
                    attempt + 1,
                    exc,
                )
        await asyncio.sleep(1.2 * (attempt + 1))

    stale = get_stale_cached_balance(cache_key, stale_ttl)
    if stale is not None:
        logger.warning(
            "USDC balance fetch failed — using stale cache $%.2f (last error: %s)",
            stale,
            last_exc,
        )
        return stale

    logger.error("All USDC balance fetches failed for %s…", pubkey[:12])
    return 0.0


async def get_onchain_usdc_balance() -> float:
    """SPL USDC balance on-chain for ``WALLET_PUBKEY`` (not Backpack)."""
    return await get_usdc_balance_robust()


async def _fetch_onchain_sol_once(pubkey: str) -> float:
    from solana.rpc.async_api import AsyncClient
    from solders.pubkey import Pubkey

    from src.core.rpc_config import call_with_rpc_fallback
    from src.core.rpc_urls import rpc_provider_label
    from src.monitoring.metrics import record_rpc_latency

    owner = Pubkey.from_string(pubkey)

    async def _fetch(rpc: str) -> float:
        t0 = time.perf_counter()
        async with AsyncClient(rpc) as client:
            resp = await client.get_balance(owner)
        record_rpc_latency(
            rpc_provider_label(rpc),
            "get_balance",
            time.perf_counter() - t0,
        )
        return int(resp.value or 0) / 1_000_000_000.0

    return float(await call_with_rpc_fallback("balance", _fetch, label="sol_balance"))


async def get_sol_balance_robust(
    wallet_pubkey: str | None = None,
    *,
    max_age_sec: int | None = None,
) -> float:
    """Native on-chain SOL with cache + stale fallback (Helius 429 safe)."""
    _ = max_age_sec
    from src.core.rpc_config import get_robust_sol_balance

    return await get_robust_sol_balance(wallet_pubkey)


async def get_sol_balance() -> float:
    """Get SOL balance (Backpack)."""
    try:
        from src.cex.backpack import get_backpack_client

        client = get_backpack_client()
        return await client.get_balance("SOL")
    except Exception as exc:
        logger.warning("Failed to get SOL balance: %s", exc)
        return 0.0


def _load_hot_keypair():
    """Load signing keypair for on-chain transfers (same sources as Jupiter)."""
    from src.core.secure_secrets import skip_hot_secret_files

    if skip_hot_secret_files():
        return None
    from src.core.security import (
        is_placeholder_secret,
        load_secrets_from_files,
        secure_load_keypair,
    )

    load_secrets_from_files()
    for env_name in ("PRIVATE_KEY_CEX_DEX", "PRIVATE_KEY"):
        material = (os.getenv(env_name) or "").strip()
        if material and not is_placeholder_secret(material):
            try:
                return secure_load_keypair(material)
            except Exception as exc:
                logger.warning("transfer_sol: %s invalid: %s", env_name, exc)
    return None


async def transfer_sol(amount_sol: float, destination: str) -> dict[str, Any]:
    """Send native SOL from the hot wallet to ``destination`` (e.g. Backpack deposit)."""
    import base64

    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solana.rpc.types import TxOpts
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    from src.core.rpc_config import call_with_rpc_fallback

    dest = (destination or "").strip()
    if not dest:
        return {"success": False, "error": "destination_missing"}
    amount = float(amount_sol)
    if amount <= 0:
        return {"success": False, "error": "amount_zero"}

    settings = get_settings()
    if settings.test_mode or settings.simulate:
        logger.info("TEST: Would transfer %.6f SOL to %s", amount, dest[:12])
        return {"success": True, "tx_sig": "simulated", "amount_sol": amount}

    keypair = _load_hot_keypair()
    if keypair is None:
        return {"success": False, "error": "signing_keypair_missing"}

    lamports = int(amount * 1_000_000_000)
    if lamports < 1:
        return {"success": False, "error": "lamports_zero"}

    try:
        to_pubkey = Pubkey.from_string(dest)
    except Exception as exc:
        return {"success": False, "error": f"invalid_destination: {exc}"}

    async def _blockhash(rpc: str):
        async with AsyncClient(rpc) as client:
            resp = await client.get_latest_blockhash()
            return resp.value.blockhash

    try:
        blockhash = await call_with_rpc_fallback(
            "transaction",
            _blockhash,
            label="sol_transfer_blockhash",
        )
        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=to_pubkey,
                lamports=lamports,
            )
        )
        msg = MessageV0.try_compile(keypair.pubkey(), [ix], [], blockhash)
        signed = VersionedTransaction(msg, [keypair])
        raw = bytes(signed)

        async def _send(rpc: str) -> str:
            async with AsyncClient(rpc) as client:
                resp = await client.send_raw_transaction(
                    raw,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )
                return str(resp.value)

        tx_sig = await call_with_rpc_fallback(
            "transaction",
            _send,
            label="sol_transfer_send",
        )
        logger.info(
            "SOL transfer sent | amount=%.6f dest=%s… tx=%s",
            amount,
            dest[:12],
            tx_sig,
        )
        return {"success": True, "tx_sig": tx_sig, "amount_sol": amount}
    except Exception as exc:
        logger.warning("SOL transfer failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def transfer_usdc(amount_usdc: float, destination: str) -> dict[str, Any]:
    """Send SPL USDC from the hot wallet to ``destination`` (e.g. Backpack deposit)."""
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solana.rpc.types import TxOpts
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        TransferCheckedParams,
        create_idempotent_associated_token_account,
        get_associated_token_address,
        transfer_checked,
    )

    from src.core.rpc_config import call_with_rpc_fallback

    dest = (destination or "").strip()
    if not dest:
        return {"success": False, "error": "destination_missing"}
    amount = float(amount_usdc)
    if amount <= 0:
        return {"success": False, "error": "amount_zero"}

    settings = get_settings()
    if settings.test_mode or settings.simulate:
        logger.info("TEST: Would transfer %.2f USDC to %s", amount, dest[:12])
        return {"success": True, "tx_sig": "simulated", "amount_usdc": amount}

    keypair = _load_hot_keypair()
    if keypair is None:
        return {"success": False, "error": "signing_keypair_missing"}

    amount_micro = int(round(amount * 1_000_000))
    if amount_micro < 1:
        return {"success": False, "error": "amount_micro_zero"}

    on_chain = await get_onchain_usdc_balance()
    reserve = float(os.getenv("V2_ONCHAIN_USDC_RESERVE", "180"))
    if on_chain - amount < reserve:
        return {
            "success": False,
            "error": "insufficient_onchain_usdc",
            "on_chain_usdc": on_chain,
            "amount_usdc": amount,
            "reserve_usdc": reserve,
        }

    try:
        dest_owner = Pubkey.from_string(dest)
    except Exception as exc:
        return {"success": False, "error": f"invalid_destination: {exc}"}

    mint = Pubkey.from_string(USDC_MINT)
    source_ata = get_associated_token_address(keypair.pubkey(), mint)
    dest_ata = get_associated_token_address(dest_owner, mint)

    async def _blockhash(rpc: str):
        async with AsyncClient(rpc) as client:
            resp = await client.get_latest_blockhash()
            return resp.value.blockhash

    try:
        blockhash = await call_with_rpc_fallback(
            "transaction",
            _blockhash,
            label="usdc_transfer_blockhash",
        )
        ixs = [
            create_idempotent_associated_token_account(
                keypair.pubkey(),
                dest_owner,
                mint,
            ),
            transfer_checked(
                TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=source_ata,
                    mint=mint,
                    dest=dest_ata,
                    owner=keypair.pubkey(),
                    amount=amount_micro,
                    decimals=6,
                    signers=[],
                )
            ),
        ]
        msg = MessageV0.try_compile(keypair.pubkey(), ixs, [], blockhash)
        signed = VersionedTransaction(msg, [keypair])
        raw = bytes(signed)

        async def _send(rpc: str) -> str:
            async with AsyncClient(rpc) as client:
                resp = await client.send_raw_transaction(
                    raw,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )
                return str(resp.value)

        tx_sig = await call_with_rpc_fallback(
            "transaction",
            _send,
            label="usdc_transfer_send",
        )
        logger.info(
            "USDC transfer sent | amount=$%.2f dest=%s… tx=%s",
            amount,
            dest[:12],
            tx_sig,
        )
        return {
            "success": True,
            "tx_sig": tx_sig,
            "amount_usdc": amount,
            "amount_micro": amount_micro,
        }
    except Exception as exc:
        logger.warning("USDC transfer failed: %s", exc)
        return {"success": False, "error": str(exc)}


def get_wallet_pubkey() -> str:
    """
    Effective wallet pubkey for swaps and health checks.

    Ledger prod: ``WALLET_PUBKEY`` from settings/env.
    Hot-key dev: derives from ``PRIVATE_KEY`` when env pubkey is unset.
    """
    cfg = get_settings()
    expected = (cfg.wallet_pubkey or cfg.WALLET_PUBKEY or os.getenv("WALLET_PUBKEY", "")).strip()
    if expected:
        return expected

    material = (cfg.PRIVATE_KEY or os.getenv("PRIVATE_KEY", "")).strip()
    if not material:
        return ""

    from src.core.security import secure_load_keypair

    return str(secure_load_keypair(material).pubkey())


async def initialize_wallet() -> None:
    """Run at startup — log CEX wallet balances and on-chain SOL when available."""
    usdc = await get_usdc_balance()
    sol_cex = await get_sol_balance()
    chain_sol = 0.0
    try:
        from src.core.capital_preflight import get_ledger_sol_balance

        chain_sol = await get_ledger_sol_balance()
    except Exception as exc:
        logger.debug("Chain SOL balance skipped at init: %s", exc)
    pubkey = get_wallet_pubkey()
    if pubkey:
        logger.info(
            "Wallet initialized | pubkey=%s… | USDC: $%.2f | SOL_cex=%.4f | SOL_chain=%.4f",
            pubkey[:12],
            usdc,
            sol_cex,
            chain_sol,
        )
    else:
        logger.info(
            "Wallet initialized | USDC: $%.2f | SOL_cex=%.4f | SOL_chain=%.4f",
            usdc,
            sol_cex,
            chain_sol,
        )
    try:
        from src.monitoring.cex_health import record_backpack_balances

        record_backpack_balances(usdc, sol_cex)
    except Exception:
        pass

_wallet_safety: WalletSafety | None = None


def _state_path() -> Path:
    from src.config.settings import settings

    return Path(settings.WALLET_SAFETY_STATE_PATH)


def _reconcile_state_path() -> Path:
    return Path(os.getenv("INVENTORY_RECONCILE_STATE_PATH", "logs/inventory_reconcile_state.json"))


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _utc_trade_hour() -> str:
    """Rolling UTC hour bucket for rate limiting."""
    return datetime.now(UTC).strftime("%Y-%m-%d-%H")


def _load_state() -> dict:
    if not _ws.get_state().get("last_update"):
        _ws.load_safety_state()
    return _ws.get_state()


def _default_state() -> dict:
    return {
        "successful_sim_count": 0,
        "last_live_trade_ts": 0.0,
        "daily_volume_usdc_micro": 0,
        "daily_volume_date": "",
        "trade_hour_bucket": "",
        "trades_this_hour": 0,
        "equity_high_water_usd": 0.0,
        "last_equity_usd": 0.0,
        "last_global_safety_ts": 0.0,
    }


def _save_state(data: dict) -> None:
    _ws.merge_state(data)


class WalletSafety:
    """Global safety coordinator (drawdown, reconcile, circuit breaker)."""

    def __init__(self) -> None:
        self.circuit_breaker = circuit_breaker

    @classmethod
    def get(cls) -> WalletSafety:
        global _wallet_safety
        if _wallet_safety is None:
            _wallet_safety = cls()
        return _wallet_safety

    def _equity_baseline_usd(self) -> float:
        try:
            return float(os.getenv("WALLET_EQUITY_BASELINE_USD", "0"))
        except (TypeError, ValueError):
            return 0.0

    def current_equity_usd(self) -> float:
        """Baseline + cumulative realized PnL from ``brain_pnl`` state."""
        try:
            from src.strategies.brain_pnl import realized_pnl_sum_all

            pnl = realized_pnl_sum_all()
        except Exception:
            pnl = 0.0
        return self._equity_baseline_usd() + float(pnl)

    def update_equity_watermark(self) -> float:
        cur = self.current_equity_usd()
        s = _load_state()
        peak = float(s.get("equity_high_water_usd") or 0.0)
        if peak <= 0:
            peak = cur
        s["equity_high_water_usd"] = max(peak, cur)
        s["last_equity_usd"] = cur
        _save_state(s)
        return cur

    def drawdown_pct(self) -> float:
        s = _load_state()
        peak = float(s.get("equity_high_water_usd") or 0.0)
        cur = float(s.get("last_equity_usd") or self.current_equity_usd())
        if peak <= 0:
            return 0.0
        if cur >= peak:
            return 0.0
        return (peak - cur) / peak * 100.0

    def _max_drawdown_pct(self) -> float:
        try:
            return float(os.getenv("MAX_DRAWDOWN_PCT", "5.0"))
        except (TypeError, ValueError):
            return 5.0

    def _trip_drawdown(self, dd: float) -> None:
        reason = f"max_drawdown_{dd:.2f}pct"
        self.circuit_breaker.trip(reason)
        msg = (
            f"MAX DRAWDOWN — BOT PAUSED\n"
            f"drawdown={dd:.2f}% limit={self._max_drawdown_pct():.2f}%\n"
            f"equity={self.current_equity_usd():.2f} USD"
        )
        logger.critical(msg.replace("\n", " | "))
        schedule_alert(msg)

    def _check_reconcile_strict(self) -> bool:
        """
        Stricter day-over-day / cross-wallet thresholds using last reconcile snapshot.

        Returns False when drift should pause the bot.
        """
        if os.getenv("INVENTORY_RECONCILE_STRICT_MODE", "true").lower() not in ("1", "true", "yes"):
            return True

        p = _reconcile_state_path()
        if not p.is_file():
            return True

        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return True
        except Exception:
            return True

        try:
            dod_thresh = float(
                os.getenv(
                    "INVENTORY_RECONCILE_STRICT_DELTA_SOL",
                    os.getenv("INVENTORY_RECONCILE_ALERT_DELTA_SOL", "0.15"),
                )
            )
        except (TypeError, ValueError):
            dod_thresh = 0.15

        try:
            cross_thresh = float(
                os.getenv(
                    "INVENTORY_RECONCILE_STRICT_CROSS_DELTA_SOL",
                    os.getenv("INVENTORY_RECONCILE_CROSS_ALERT_DELTA_SOL", "0.35"),
                )
            )
        except (TypeError, ValueError):
            cross_thresh = 0.35

        prior_cex = state.get("prior_cex_sol")
        cex_sol = state.get("last_cex_sol")
        chain_sol = state.get("last_chain_sol")

        if (
            isinstance(prior_cex, (int, float))
            and isinstance(cex_sol, (int, float))
            and dod_thresh > 0
        ):
            delta = abs(float(cex_sol) - float(prior_cex))
            if delta > dod_thresh:
                msg = (
                    f"STRICT INVENTORY RECONCILE: |Δ CEX SOL|={delta:.4f} > {dod_thresh:.4f}\n"
                    f"last_run={state.get('last_run_utc', 'n/a')}"
                )
                logger.critical(msg.replace("\n", " | "))
                schedule_alert(msg)
                self.circuit_breaker.trip("inventory_dod_drift")
                return False

        if (
            isinstance(cex_sol, (int, float))
            and isinstance(chain_sol, (int, float))
            and cross_thresh > 0
        ):
            cex_f = float(cex_sol)
            chain_f = float(chain_sol)
            # Reverse arb: SOL on Backpack (CEX sell) + on-chain (fees/Jupiter) is expected.
            min_cex_sol = float(os.getenv("INVENTORY_CROSS_MIN_CEX_SOL", "0.1"))
            if os.getenv("INVENTORY_ALLOW_CHAIN_SOL_HOLDING", "true").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                if cex_f < min_cex_sol and chain_f >= min_cex_sol:
                    return True
                if cex_f >= min_cex_sol and chain_f >= min_cex_sol:
                    return True

            delta_x = abs(cex_f - chain_f)
            if delta_x > cross_thresh:
                msg = (
                    f"STRICT INVENTORY RECONCILE: |CEX−chain SOL|={delta_x:.4f} > {cross_thresh:.4f}\n"
                    f"cex={float(cex_sol):.4f} chain={float(chain_sol):.4f}"
                )
                logger.critical(msg.replace("\n", " | "))
                schedule_alert(msg)
                self.circuit_breaker.trip("inventory_cross_drift")
                return False

        return True

    def check_global_safety(self) -> bool:
        """
        Equity drawdown gate + wallet_safety state + reconcile + circuit breaker.

        Call periodically from the monitor loop and before live sends.
        """
        self.update_equity_watermark()
        dd = self.drawdown_pct()
        _ws.set_drawdown_pct(dd)

        s = _load_state()
        s["last_global_safety_ts"] = time.time()
        _save_state(s)

        if not _ws.check_global_safety():
            if dd > self._max_drawdown_pct() and not self.circuit_breaker.is_tripped:
                self._trip_drawdown(dd)
            return False

        if not self._check_reconcile_strict():
            return False

        if self.circuit_breaker.should_pause():
            return False

        return True

    def safety_status(self) -> dict[str, Any]:
        return {
            "drawdown_pct": round(self.drawdown_pct(), 4),
            "max_drawdown_pct": self._max_drawdown_pct(),
            "equity_usd": round(self.current_equity_usd(), 2),
            "equity_high_water_usd": float(_load_state().get("equity_high_water_usd") or 0),
            "circuit_breaker": self.circuit_breaker.status(),
            "global_ok": not self.circuit_breaker.should_pause(),
        }


def wallet_safety() -> WalletSafety:
    return WalletSafety.get()


def check_global_safety() -> bool:
    return WalletSafety.get().check_global_safety()


# Aliases for strategy modules (wallet_safety naming)
check_safety = check_global_safety


def record_successful_simulation() -> None:
    _ws.record_successful_simulation()


def record_live_trade_usdc_micro(amount_micro: int) -> None:
    _ws.record_live_trade_usdc_micro(amount_micro)
    WalletSafety.get().update_equity_watermark()


def record_cex_reconciliation(delta_sol: float) -> None:
    _ws.record_cex_reconciliation(delta_sol)


def simulation_count() -> int:
    return _ws.simulation_count()


def load_safety_state() -> dict[str, Any]:
    WalletSafety.get()
    return _ws.load_safety_state()


def before_live_send(usdc_amount_micro: int) -> tuple[bool, str]:
    if circuit_breaker.should_pause():
        reason = circuit_breaker.trip_reason or "circuit_breaker_pause"
        logger.warning("Wallet safety: blocking live send (%s)", reason)
        return False, reason
    ok, reason = _ws.before_live_send(usdc_amount_micro)
    if not ok:
        return ok, reason
    max_flash = max(0, int(os.getenv("MAX_SINGLE_TRADE_USDC_MICRO", "0")))
    if max_flash > 0 and usdc_amount_micro > max_flash:
        msg = f"max_single_trade_{usdc_amount_micro}>{max_flash}"
        logger.warning("Wallet safety: blocking live send (%s)", msg)
        return False, msg
    return True, "ok"


async def global_safety_monitor_loop(interval_sec: float | None = None) -> None:
    """Background task: periodic ``check_global_safety`` (drawdown + reconcile snapshot)."""
    interval = interval_sec
    if interval is None:
        try:
            interval = float(os.getenv("GLOBAL_SAFETY_CHECK_INTERVAL_SEC", "60"))
        except (TypeError, ValueError):
            interval = 60.0
    interval = max(15.0, float(interval))

    ws = WalletSafety.get()
    while True:
        try:
            ok = ws.check_global_safety()
            if not ok:
                logger.warning(
                    "Global safety check failed | drawdown=%.2f%% breaker=%s",
                    ws.drawdown_pct(),
                    ws.circuit_breaker.trip_reason,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("global_safety_monitor_loop: %s", exc)
        await asyncio.sleep(interval)
