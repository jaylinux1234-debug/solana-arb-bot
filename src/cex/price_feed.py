# cex_price_feed.py
import asyncio
import logging
import os
import random
import time
from typing import Any

from src.cex.ccxt_wrapper import (
    create_backpack_exchange,
    create_bybit_exchange,
    create_kucoin_exchange,
    create_okx_exchange,
    discovery_venue_factories,
)

logger = logging.getLogger(__name__)


def _cex_skip_venues() -> set[str]:
    raw = (os.getenv("CEX_SKIP_VENUES") or "bybit").strip()
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


def _candidate_symbols(symbol: str) -> list[str]:
    """Try USDC pair first, then USDT when enabled (many venues lack */USDC)."""
    out = [symbol]
    if os.getenv("CEX_SYMBOL_USDT_FALLBACK", "true").lower() not in ("1", "true", "yes"):
        return out
    if symbol.endswith("/USDC"):
        alt = f"{symbol[:-5]}/USDT"
        if alt not in out:
            out.append(alt)
    return out


def _short_exc(exc: BaseException, limit: int = 120) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    return msg if len(msg) <= limit else msg[: limit - 3] + "..."


class CexPriceFeed:
    """
    Price discovery: Backpack → Bybit → OKX → KuCoin (no Binance).
    Trading / orders / withdrawals stay on Backpack via ``cex_executor`` only.
    """

    def __init__(self) -> None:
        self.primary_venue = os.getenv("CEX_PRICE_FEED_PRIMARY", "backpack").lower()

        self._backpack = None
        self._bybit = None
        self._okx = None
        self._kucoin = None

        # Successful mid prices only (robust TTL).
        self._ok_cache: dict[str, dict[str, Any]] = {}
        self.cache_ttl = max(5, int(os.getenv("CEX_CACHE_TTL_SECONDS", "30")))

        self.fail_log_interval = max(
            30.0, float(os.getenv("CEX_PRICE_FAIL_LOG_INTERVAL_SECONDS", "180"))
        )
        self._fail_warn_ts: dict[str, float] = {}

        self.batch_log_interval = max(
            10.0, float(os.getenv("CEX_PRICE_BATCH_LOG_INTERVAL_SECONDS", "45"))
        )
        self._last_batch_key: tuple[int, int] | None = None
        self._last_batch_log_ts: float = 0.0
        self._batch_sem = asyncio.Semaphore(max(1, int(os.getenv("CEX_PRICE_MAX_CONCURRENT", "3"))))

        # Last venue that supplied a good price (for strategy diagnostics).
        self.last_success_venue: dict[str, str] = {}
        self._skip_venues = _cex_skip_venues()

        logger.info(
            "CEX price feed | discovery: Backpack → Bybit → OKX → KuCoin | "
            "cache_ttl=%ss | fail_log_interval=%ss",
            self.cache_ttl,
            int(self.fail_log_interval),
        )

    def _get_backpack(self):
        if self._backpack is None:
            self._backpack = create_backpack_exchange()
        return self._backpack

    def _get_bybit(self):
        if self._bybit is None:
            self._bybit = create_bybit_exchange()
        return self._bybit

    def _get_okx(self):
        if self._okx is None:
            self._okx = create_okx_exchange()
        return self._okx

    def _get_kucoin(self):
        if self._kucoin is None:
            self._kucoin = create_kucoin_exchange()
        return self._kucoin

    def _discovery_venues(self):
        return discovery_venue_factories(skip_venues=self._skip_venues)

    async def _fetch_ticker(self, exchange, name: str, symbol: str) -> tuple[float | None, float]:
        """
        Return (mid_price, bid_ask_spread_bps).

        Spread bps uses (ask - bid) / mid × 10_000 when the book is present; otherwise 0.0.
        """
        try:
            ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
            bid = ticker.get("bid")
            ask = ticker.get("ask")
            last = ticker.get("last")

            if bid and ask and float(bid) > 0 and float(ask) > 0:
                b, a = float(bid), float(ask)
                mid = (b + a) / 2.0
                spread_bps = max(0.0, (a - b) / mid * 10000.0) if mid > 0 else 0.0
                return mid, spread_bps
            if last and float(last) > 0:
                return float(last), 0.0
        except Exception as exc:
            logger.debug("CEX %s %s: %s", name, symbol, _short_exc(exc))
        return None, 0.0

    async def get_price_and_volatility_bps(
        self, symbol: str = "SOL/USDC"
    ) -> tuple[float | None, float]:
        """Best-effort mid plus short-term volatility proxy (CEX bid–ask width in bps)."""
        now = time.time()

        hit = self._ok_cache.get(symbol)
        if hit and now - hit["ts"] < 45:
            return hit["price"], float(hit.get("volatility_bps", 0.0))

        for sym_try in _candidate_symbols(symbol):
            for i, (name, factory) in enumerate(self._discovery_venues()):
                if i > 0:
                    await asyncio.sleep(0.3 + random.uniform(0, 0.4))
                mid, vol_bps = await self._fetch_ticker(factory(), name, sym_try)
                if mid and mid > 0:
                    self._ok_cache[symbol] = {
                        "price": mid,
                        "volatility_bps": vol_bps,
                        "ts": now,
                    }
                    self.last_success_venue[symbol] = name
                    return mid, vol_bps

        if now - self._fail_warn_ts.get(symbol, 0) > 300:
            self._fail_warn_ts[symbol] = now
            logger.warning("All CEX venues failed for %s", symbol)
        else:
            logger.debug("All CEX venues failed for %s (suppressed)", symbol)

        return None, 0.0

    async def get_price(self, symbol: str = "SOL/USDC") -> float | None:
        """Best-effort mid from discovery venues; 45s fresh cache hit."""
        mid, _ = await self.get_price_and_volatility_bps(symbol)
        return mid

    async def get_bid_ask_mid(
        self, symbol: str = "SOL/USDC"
    ) -> tuple[float | None, float | None, float | None]:
        """Best-effort (bid, ask, mid) from discovery venues."""
        for sym_try in _candidate_symbols(symbol):
            for i, (name, factory) in enumerate(self._discovery_venues()):
                if i > 0:
                    await asyncio.sleep(0.3 + random.uniform(0, 0.4))
                try:
                    ticker = await asyncio.to_thread(factory().fetch_ticker, sym_try)
                except Exception as exc:
                    logger.debug("CEX %s %s bid/ask: %s", name, sym_try, _short_exc(exc))
                    continue
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                last = ticker.get("last")
                if bid and ask and float(bid) > 0 and float(ask) > 0:
                    b, a = float(bid), float(ask)
                    return b, a, (b + a) / 2.0
                if last and float(last) > 0:
                    m = float(last)
                    return m, m, m
        return None, None, None

    async def get_multiple_prices(self, symbols: list[str]) -> dict[str, float]:
        async def _one(sym: str):
            async with self._batch_sem:
                return sym, await self.get_price(sym)

        results = await asyncio.gather(
            *[_one(s) for s in symbols],
            return_exceptions=True,
        )

        out: dict[str, float] = {}
        exc_count = 0
        for item in results:
            if isinstance(item, Exception):
                exc_count += 1
                logger.debug("CEX get_price raised: %s", _short_exc(item))
                continue
            sym, res = item
            if isinstance(res, Exception):
                exc_count += 1
                logger.debug("CEX get_price raised for %s: %s", sym, _short_exc(res))
                continue
            if isinstance(res, (int, float)) and res and float(res) > 0:
                out[sym] = float(res)

        n_ok, n_tot = len(out), len(symbols)
        now = time.time()
        key = (n_ok, n_tot)
        changed = key != self._last_batch_key
        due = now - self._last_batch_log_ts >= self.batch_log_interval

        if exc_count:
            logger.debug(
                "CEX batch: %s/%s pairs OK (%s exceptions during fetch)",
                n_ok,
                n_tot,
                exc_count,
            )
        if changed or due:
            logger.info(
                "CEX prices fetched: %s/%s pairs",
                n_ok,
                n_tot,
            )
            self._last_batch_key = key
            self._last_batch_log_ts = now
        else:
            logger.debug("CEX prices fetched: %s/%s pairs (unchanged)", n_ok, n_tot)

        return out


# Global singleton
cex_feed = CexPriceFeed()


async def get_price(symbol: str = "SOL/USDC") -> float | None:
    """Compatibility helper for callers that expect a module-level price fetch."""
    return await cex_feed.get_price(symbol)
