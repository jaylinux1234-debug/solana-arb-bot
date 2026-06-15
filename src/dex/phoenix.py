# src/dex/phoenix.py — Phoenix V1 on-chain orderbook oracle (SOL/USDC and aliases).
"""
Use Phoenix V1 for tight on-chain CEX-like SOL/USDC pricing.

Primary path: deserialize market account via ``phoenixpy`` (optional install).
Fallback: Jupiter quotes restricted to ``dexes=Phoenix``.

Install on-chain parser (optional):
  pip install git+https://github.com/Ellipsis-Labs/phoenixpy.git
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# Mainnet Phoenix V1 (Ellipsis Labs)
PHOENIX_PROGRAM = "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY"
PHOENIX_SOL_USDC_MARKET = "4DoNfFBfF7UokCC2FQzriy7yHK6DY6NVdYpuekQ5pRgg"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

# Aliases for ``get_phoenix_bid_ask(market)``
PHOENIX_MARKETS: dict[str, str] = {
    "SOL_USDC": PHOENIX_SOL_USDC_MARKET,
    "SOL/USDC": PHOENIX_SOL_USDC_MARKET,
    "SOL-USDC": PHOENIX_SOL_USDC_MARKET,
}


@dataclass(frozen=True)
class PhoenixQuote:
    bid: float
    ask: float
    mid: float
    market: str
    source: str  # "phoenixpy" | "jupiter_phoenix"


def _enabled() -> bool:
    try:
        from src.config.settings import settings

        if hasattr(settings, "ENABLE_PHOENIX_V1"):
            return bool(settings.ENABLE_PHOENIX_V1)
        if hasattr(settings, "enable_phoenix_v1"):
            return bool(settings.enable_phoenix_v1)
    except Exception:
        pass
    return os.getenv("ENABLE_PHOENIX_V1", "false").lower() in ("1", "true", "yes")


def resolve_market_pubkey(market: str) -> Pubkey:
    key = (market or "").strip()
    if not key:
        return Pubkey.from_string(PHOENIX_SOL_USDC_MARKET)
    if key in PHOENIX_MARKETS:
        return Pubkey.from_string(PHOENIX_MARKETS[key])
    try:
        return Pubkey.from_string(key)
    except Exception as exc:
        raise ValueError(f"Unknown Phoenix market: {market}") from exc


def _rpc_url() -> str:
    return (os.getenv("SOLANA_RPC_URL_FAST") or "").strip() or (
        os.getenv("SOLANA_RPC_URL") or ""
    ).strip()


async def _fetch_market_account_bytes(
    market_pubkey: Pubkey,
    client: AsyncClient,
) -> bytes | None:
    resp = await client.get_account_info(market_pubkey)
    value = resp.value
    if value is None or value.data is None:
        return None
    data = value.data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, tuple) and data:
        return bytes(data[0])
    return None


def _parse_bid_ask_with_phoenixpy(market_pubkey: Pubkey, raw: bytes) -> PhoenixQuote | None:
    try:
        from src.dex.phoenix_solana_compat import ensure_phoenix_import_compat

        ensure_phoenix_import_compat()
        from phoenix.market import Market  # phoenix-trade package
    except ImportError:
        return None

    try:
        mkt = Market.deserialize_market_data(market_pubkey, raw)
        ladder = mkt.get_ui_ladder(
            levels=max(1, int(os.getenv("PHOENIX_LADDER_LEVELS", "1"))),
        )
        bid = float(ladder.bids[0].price) if ladder.bids else None
        ask = float(ladder.asks[0].price) if ladder.asks else None
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2.0
        return PhoenixQuote(
            bid=bid,
            ask=ask,
            mid=mid,
            market=str(market_pubkey),
            source="phoenixpy",
        )
    except Exception as exc:
        logger.debug("phoenixpy parse failed: %s", exc)
        return None


async def _jupiter_phoenix_bid_ask(
    *,
    probe_usdc_micro: int,
    probe_sol_lamports: int,
) -> PhoenixQuote | None:
    """Infer bid/ask from Jupiter routes limited to Phoenix."""
    from src.dex.quote import get_jupiter_quote

    slippage = int(os.getenv("PHOENIX_JUPITER_SLIPPAGE_BPS", "50"))
    dexes = (os.getenv("PHOENIX_JUPITER_DEXES") or "Phoenix").strip()

    buy_sol = await get_jupiter_quote(
        USDC_MINT,
        SOL_MINT,
        probe_usdc_micro,
        slippage_bps=slippage,
        dexes=dexes,
    )
    sell_sol = await get_jupiter_quote(
        SOL_MINT,
        USDC_MINT,
        probe_sol_lamports,
        slippage_bps=slippage,
        dexes=dexes,
    )
    if not buy_sol or not sell_sol:
        return None
    try:
        sol_out = int(buy_sol["outAmount"])
        usdc_out = int(sell_sol["outAmount"])
    except (KeyError, TypeError, ValueError):
        return None
    if sol_out <= 0 or usdc_out <= 0:
        return None

    usdc_in = probe_usdc_micro / 1_000_000
    sol_in = probe_sol_lamports / 1_000_000_000
    ask_px = usdc_in / (sol_out / 1_000_000_000)  # buy SOL (pay USDC per SOL)
    bid_px = (usdc_out / 1_000_000) / sol_in  # sell SOL (receive USDC per SOL)
    if bid_px <= 0 or ask_px <= 0:
        return None
    mid = (bid_px + ask_px) / 2.0
    return PhoenixQuote(
        bid=bid_px,
        ask=ask_px,
        mid=mid,
        market=PHOENIX_SOL_USDC_MARKET,
        source="jupiter_phoenix",
    )


async def get_phoenix_bid_ask(
    market: str = "SOL_USDC",
    *,
    client: AsyncClient | None = None,
    probe_usdc_micro: int | None = None,
    probe_sol_lamports: int | None = None,
) -> tuple[float | None, float | None, PhoenixQuote | None]:
    """
    Return ``(bid, ask, PhoenixQuote)`` for a Phoenix V1 market.

    ``bid`` / ``ask`` are USDC per SOL (same convention as CEX/Jupiter probes).
    Returns ``(None, None, None)`` when disabled, missing RPC, or parse failure.
    """
    if not _enabled():
        return None, None, None

    rpc = _rpc_url()
    if not rpc:
        logger.warning("Phoenix: SOLANA_RPC_URL / SOLANA_RPC_URL_FAST unset")
        return None, None, None

    own_client = client is None
    if own_client:
        client = AsyncClient(rpc)

    market_pubkey = resolve_market_pubkey(market)
    quote: PhoenixQuote | None = None

    try:
        raw = await _fetch_market_account_bytes(market_pubkey, client)
        if raw:
            quote = _parse_bid_ask_with_phoenixpy(market_pubkey, raw)

        if quote is None and os.getenv("PHOENIX_JUPITER_FALLBACK", "true").lower() in (
            "1",
            "true",
            "yes",
        ):
            probe_usdc = probe_usdc_micro or int(os.getenv("PHOENIX_PROBE_USDC_MICRO", "50000000"))
            probe_sol = probe_sol_lamports or int(
                os.getenv("PHOENIX_PROBE_SOL_LAMPORTS", "100000000")
            )
            quote = await _jupiter_phoenix_bid_ask(
                probe_usdc_micro=probe_usdc,
                probe_sol_lamports=probe_sol,
            )
            if quote:
                logger.debug("Phoenix bid/ask via Jupiter fallback (dexes=Phoenix)")

        if quote:
            return quote.bid, quote.ask, quote
        return None, None, None
    finally:
        if own_client:
            await client.close()


async def get_phoenix_mid_usdc_per_sol(
    market: str = "SOL_USDC",
    *,
    client: AsyncClient | None = None,
) -> float | None:
    """Convenience: mid price only."""
    _, _, q = await get_phoenix_bid_ask(market, client=client)
    return q.mid if q else None


def _probe_usdc_micro() -> int:
    return int(os.getenv("PHOENIX_PROBE_USDC_MICRO", "12000000"))


def _market_label() -> str:
    return (os.getenv("PHOENIX_MARKET_LABEL") or "SOL_USDC").strip()


class PhoenixExecutor:
    """
    Phoenix V1 swap quotes for the DEX leg (SOL/USDC).

    Uses ``phoenix_trade.PhoenixClient`` when installed; falls back to
    on-chain ladder parse / Jupiter-Phoenix routes in this module.
    """

    def __init__(self) -> None:
        self._market_label = _market_label()
        self._market_pubkey = (
            os.getenv("PHOENIX_SOL_USDC_MARKET") or PHOENIX_SOL_USDC_MARKET
        ).strip()
        self._probe_micro = _probe_usdc_micro()
        self._client: Any | None = None
        self._client_kind: str | None = None
        if _enabled():
            self._init_client()

    def _init_client(self) -> None:
        """``phoenix-trade`` installs as the ``phoenix`` package (on-chain ``Market`` parser)."""
        try:
            from src.dex.phoenix_solana_compat import ensure_phoenix_import_compat

            ensure_phoenix_import_compat()
            from phoenix.market import Market

            self._client = Market
            self._client_kind = "phoenix_trade"
            logger.info("PhoenixExecutor: phoenix-trade (phoenix.market) ready")
            return
        except ImportError as exc:
            logger.debug("phoenix.market import failed: %s", exc)

        rpc = _rpc_url()
        try:
            import phoenixpy

            self._client = phoenixpy.PhoenixClient(
                market_address=self._market_pubkey,
                rpc_url=rpc or None,
            )
            self._client_kind = "phoenixpy"
            logger.info("PhoenixExecutor: phoenixpy.PhoenixClient ready")
        except ImportError:
            logger.info(
                "PhoenixExecutor: phoenix-trade not available — using on-chain RPC / Jupiter fallback"
            )

    async def get_quote(
        self,
        amount_usdc: int,
        *,
        side: str = "sell",
        market: str | None = None,
    ) -> dict[str, Any] | None:
        """Swap quote from Phoenix client API (package-specific shape)."""
        if not _enabled() or self._client is None:
            return None

        mkt = market or self._market_label
        amount = int(amount_usdc)
        if self._client_kind == "phoenix_trade":
            return None

        try:
            if self._client_kind == "phoenixpy" and self._client is not None:
                raw = await self._client.get_quote(side, amount)
            else:
                return None
            if raw is None:
                return None
            if isinstance(raw, dict):
                return raw
            return {"raw": raw}
        except Exception as exc:
            logger.debug("Phoenix get_quote failed: %s", exc)
            return None

    def _usdc_per_sol_from_quote(self, quote: dict[str, Any], *, side: str) -> float | None:
        """Normalize quote payloads to USDC per SOL."""
        for key in ("price", "executionPrice", "avgPrice", "mid"):
            raw = quote.get(key)
            if raw is not None:
                try:
                    px = float(raw)
                    if px > 0:
                        return px
                except (TypeError, ValueError):
                    continue

        try:
            out_amt = int(quote.get("outAmount") or quote.get("out_amount") or 0)
            in_amt = int(quote.get("inAmount") or quote.get("in_amount") or quote.get("amount_in") or 0)
        except (TypeError, ValueError):
            return None

        if in_amt <= 0 or out_amt <= 0:
            return None

        side_l = side.lower()
        if side_l == "sell":
            # Sell SOL → USDC: in lamports, out micro-USDC
            sol_in = in_amt / 1_000_000_000.0
            usdc_out = out_amt / 1_000_000.0
            if sol_in > 0:
                return usdc_out / sol_in
        else:
            # Buy SOL with USDC
            usdc_in = in_amt / 1_000_000.0
            sol_out = out_amt / 1_000_000_000.0
            if sol_out > 0:
                return usdc_in / sol_out
        return None

    async def get_implied_usdc_per_sol(self, amount_usdc_micro: int | None = None) -> float | None:
        """
        Best USDC/SOL for the DEX sell leg (probe-sized).

        Prefer Phoenix client quote, then bid from on-chain ladder.
        """
        probe = int(amount_usdc_micro or self._probe_micro)
        quote = await self.get_quote(probe, side="sell")
        if quote:
            px = self._usdc_per_sol_from_quote(quote, side="sell")
            if px and px > 0:
                return px

        bid, _ask, pq = await get_phoenix_bid_ask(
            self._market_label,
            probe_usdc_micro=probe,
        )
        if bid and bid > 0:
            return float(bid)
        if pq and pq.mid > 0:
            return float(pq.mid)
        return None


_phoenix_executor: PhoenixExecutor | None = None


def get_phoenix_executor() -> PhoenixExecutor:
    global _phoenix_executor
    if _phoenix_executor is None:
        _phoenix_executor = PhoenixExecutor()
    return _phoenix_executor


async def best_dex_usdc_per_sol_for_sell(
    probe_usdc_micro: int,
    *,
    jupiter_usdc_per_sol: float | None = None,
) -> tuple[float | None, str]:
    """
    Pick the better DEX sell price (USDC per SOL) for CEX-buy → DEX-sell arb.

    Returns ``(price, source)`` with source ``phoenix`` | ``jupiter``.
    """
    from src.dex.executor import get_dex_executor

    picked = await get_dex_executor().get_best_dex_price(
        probe_usdc_micro,
        use_phoenix=_enabled(),
        jupiter_price=jupiter_usdc_per_sol,
    )
    if picked and picked.price > 0:
        return picked.price, picked.venue
    return None, "jupiter"
