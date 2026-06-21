# src/cex/backpack.py
"""
Backpack Exchange client — single async production client.

Public market data (ticker, depth) does not require auth.
Private endpoints use Backpack ED25519 signing (see docs.backpack.exchange).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal, ROUND_DOWN, getcontext
from pathlib import Path
from typing import Any, Awaitable, Callable

getcontext().prec = 28

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_MS = 5000
DEFAULT_WS_URL = "wss://ws.backpack.exchange"
_DEFAULT_SOL_QUANTITY_STEP = Decimal(
    os.getenv("BACKPACK_SOL_QUANTITY_STEP", "0.01")
)
_DEFAULT_SOL_MIN_QUANTITY = Decimal(
    os.getenv("BACKPACK_SOL_MIN_QUANTITY", "0.01")
)
_SECRET_FILE_ENV = {
    "BACKPACK_API_KEY": "BACKPACK_API_KEY_FILE",
    "BACKPACK_SECRET": "BACKPACK_SECRET_FILE",
}


def _is_encrypted_secret_blob(raw: str) -> bool:
    s = (raw or "").lstrip()
    return s.startswith("{") and ("ENC[" in s or '"sops"' in s)


def get_secret(name: str, settings: Settings | None = None) -> str:
    """Load Backpack (or other) credentials from env / Docker secret files."""
    cfg = settings or get_settings()
    file_env = _SECRET_FILE_ENV.get(name, f"{name}_FILE")
    return _load_credential(name, file_env, cfg)


def _load_credential(env_name: str, file_env: str, settings: Settings) -> str:
    """Resolve API material from env or secret file; skip SOPS ciphertext blobs."""
    from src.core.security import is_placeholder_secret, load_secrets_from_files, read_secret_file

    load_secrets_from_files()

    value = (os.getenv(env_name) or getattr(settings, env_name, None) or "").strip()
    if value and not _is_encrypted_secret_blob(value) and not is_placeholder_secret(value):
        return value

    path = (os.getenv(file_env) or getattr(settings, file_env, None) or "").strip()
    if not path:
        return ""

    try:
        raw = read_secret_file(Path(path))
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return ""

    if _is_encrypted_secret_blob(raw):
        logger.warning(
            "%s file looks SOPS-encrypted — run `npm run secrets:sync-local` or decrypt before live trading",
            env_name,
        )
        return ""

    if is_placeholder_secret(raw):
        return ""
    return raw.strip()


def _build_signing_string(
    instruction: str,
    params: dict[str, Any],
    timestamp_ms: int,
    window_ms: int,
) -> str:
    parts: list[str] = []
    for key in sorted(params.keys()):
        val = params[key]
        if val is None:
            continue
        if isinstance(val, bool):
            text = str(val).lower()
        else:
            text = str(val)
        parts.append(f"{key}={text}")
    body = "&".join(parts)
    if body:
        return f"instruction={instruction}&{body}&timestamp={timestamp_ms}&window={window_ms}"
    return f"instruction={instruction}&timestamp={timestamp_ms}&window={window_ms}"


def _decimals_from_step(step: Decimal) -> int:
    text = format(step.normalize(), "f")
    if "." not in text:
        return 0
    frac = text.split(".", 1)[1].rstrip("0")
    return len(frac)


def is_invalid_quantity_error(error: str) -> bool:
    text = (error or "").upper()
    return (
        "INVALID_QUANTITY" in text
        or "QUANTITY DECIMAL TOO LONG" in text
    )


def sanitize_quantity(
    qty: float,
    *,
    step_size: Decimal | None = None,
    min_qty: Decimal | None = None,
    side: str = "sell",
) -> tuple[float, str]:
    """Round down to Backpack ``stepSize`` and return (float, api_string)."""
    step = step_size or _DEFAULT_SOL_QUANTITY_STEP
    minimum = min_qty or _DEFAULT_SOL_MIN_QUANTITY
    if step <= 0:
        step = _DEFAULT_SOL_QUANTITY_STEP

    qty_dec = Decimal(str(qty))
    if qty_dec <= 0:
        return 0.0, "0"

    rounded = (qty_dec // step) * step
    if rounded < minimum:
        logger.warning(
            "Sanitized %s qty below min | raw=%s rounded=%s min=%s step=%s",
            side,
            qty,
            rounded,
            minimum,
            step,
        )
        return 0.0, "0"

    quant = step
    rounded = rounded.quantize(quant, rounding=ROUND_DOWN)
    final = float(rounded)
    decimals = _decimals_from_step(step)
    qty_str = f"{final:.{decimals}f}" if decimals > 0 else str(int(final))
    logger.info(
        "Sanitized %s qty: %s → %s (step=%s min=%s)",
        side,
        qty,
        qty_str,
        step,
        minimum,
    )
    return final, qty_str


def _normalize_symbol(symbol: str) -> str:
    """Backpack spot markets use ``SOL_USDC`` (not bare ``SOL``)."""
    sym = (symbol or "SOL_USDC").strip().upper().replace("/", "_")
    if sym == "SOL":
        sym = "SOL_USDC"
    elif not sym.endswith("_USDC") and "_" not in sym:
        sym = f"{sym}_USDC"
    return sym


def _ws_url() -> str:
    """Backpack public WS (see docs.backpack.exchange — not ``stream.backpack.exchange``)."""
    raw = (os.getenv("BACKPACK_WS_URL") or DEFAULT_WS_URL).strip()
    if "stream.backpack.exchange" in raw:
        logger.warning(
            "BACKPACK_WS_URL=%s is deprecated; use wss://ws.backpack.exchange",
            raw,
        )
    return raw or DEFAULT_WS_URL


def _ticker_stream_params(
    symbols: list[str],
    *,
    include_book_ticker: bool = True,
    include_ticker: bool = True,
) -> list[str]:
    """Build ``SUBSCRIBE`` stream names: ``ticker.SOL_USDC``, ``bookTicker.SOL_USDC``."""
    params: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        s = _normalize_symbol(sym)
        if not s or s in seen:
            continue
        seen.add(s)
        if include_ticker:
            params.append(f"ticker.{s}")
        if include_book_ticker:
            params.append(f"bookTicker.{s}")
    return params


class BackpackClient:
    """Backpack REST client with rate limiting, aiohttp, and ED25519 signing."""

    BASE_URL = "https://api.backpack.exchange"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = get_secret("BACKPACK_API_KEY", self.settings)
        self.secret = get_secret("BACKPACK_SECRET", self.settings)
        self.base_url = self.BASE_URL
        self._session: aiohttp.ClientSession | None = None
        self._private_key: ed25519.Ed25519PrivateKey | None = None
        self._last_request = 0.0
        self._rate_limit_delay = 0.15
        self._balance_cache: dict[str, tuple[float, float]] = {}
        self._balance_cache_ttl = float(os.getenv("CEX_BALANCE_CACHE_TTL_SEC", "2.0"))
        self._market_filters_cache: dict[str, dict[str, Decimal]] = {}
        self._ws_ticker_cache: dict[str, dict[str, Any]] = {}
        self._ws: Any | None = None

        if self.api_key and self.secret and not _is_encrypted_secret_blob(self.secret):
            try:
                seed = base64.b64decode(self.secret)
                self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
            except Exception as exc:
                logger.warning("Backpack ED25519 key load failed: %s", exc)
        elif not self.api_key:
            logger.warning("BACKPACK_API_KEY not configured — public endpoints only")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    def _auth_ready(self) -> bool:
        return bool(self.api_key and self._private_key)

    def _sign(self, instruction: str, params: dict[str, Any]) -> dict[str, str]:
        if not self._auth_ready():
            raise RuntimeError("Backpack API credentials not configured or not decrypted")

        timestamp_ms = int(time.time() * 1000)
        window_ms = DEFAULT_WINDOW_MS
        sign_str = _build_signing_string(instruction, params, timestamp_ms, window_ms)
        signature = base64.b64encode(
            self._private_key.sign(sign_str.encode("utf-8"))  # type: ignore[union-attr]
        ).decode("utf-8")

        return {
            "X-API-Key": self.api_key,
            "X-Signature": signature,
            "X-Timestamp": str(timestamp_ms),
            "X-Window": str(window_ms),
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _rate_limit(self) -> None:
        now = time.monotonic()
        wait = self._rate_limit_delay - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _public_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        from src.cex.backpack_ticker import parse_json_body

        await self._rate_limit()
        sym = (params or {}).get("symbol")
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.get(url, params=params or {}) as resp:
                if resp.status == 400 and sym:
                    logger.debug("Backpack market not listed: %s (%s)", sym, path)
                    return {}
                resp.raise_for_status()
                text = await resp.text()
        except Exception as exc:
            if sym and "400" in str(exc):
                logger.debug("Backpack public GET %s unavailable for %s", path, sym)
            else:
                logger.warning("Backpack public GET %s failed: %s", path, exc)
            return {}

        parsed = parse_json_body(text)
        if parsed is None:
            if sym:
                logger.debug("Backpack empty/invalid JSON for %s (%s)", sym, path)
            else:
                logger.debug("Backpack empty/invalid JSON (%s)", path)
            return {}
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"data": parsed}
        return {}

    async def _signed_request(
        self,
        method: str,
        path: str,
        instruction: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._auth_ready():
            logger.error("Backpack signed request skipped — credentials missing")
            return {"success": False, "error": "credentials_missing"}

        await self._rate_limit()
        sign_params = dict(body or params or {})
        headers = self._sign(instruction, sign_params)
        url = f"{self.base_url}{path}"
        session = await self._get_session()

        try:
            if method.upper() == "GET":
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status >= 400:
                        body_text = await resp.text()
                        logger.error(
                            "Backpack API %s %s -> %s: %s",
                            method,
                            path,
                            resp.status,
                            body_text[:500],
                        )
                        return {
                            "success": False,
                            "error": body_text[:500] or f"http_{resp.status}",
                            "status": resp.status,
                        }
                    data = await resp.json()
            else:
                async with session.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status >= 400:
                        body_text = await resp.text()
                        logger.error(
                            "Backpack API %s %s -> %s: %s",
                            method,
                            path,
                            resp.status,
                            body_text[:500],
                        )
                        return {
                            "success": False,
                            "error": body_text[:500] or f"http_{resp.status}",
                            "status": resp.status,
                        }
                    data = await resp.json()
            if isinstance(data, dict):
                data.setdefault("success", True)
                return data
            return {"success": True, "data": data}
        except Exception as exc:
            logger.error("Backpack request failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def get_orderbook(self, symbol: str = "SOL_USDC", limit: int = 10) -> dict[str, Any]:
        sym = _normalize_symbol(symbol)
        try:
            return await self._public_get(
                "/api/v1/depth",
                {"symbol": sym, "limit": str(limit)},
            )
        except Exception as exc:
            logger.error("Orderbook failed: %s", exc)
            return {}

    async def get_order_book(
        self, symbol: str = "SOL_USDC", depth: int = 20
    ) -> dict[str, Any]:
        """Order book depth (``depth`` levels)."""
        return await self.get_orderbook(_normalize_symbol(symbol), limit=depth)

    async def get_depth(
        self, market: str = "SOL_USDC", limit: int = 5
    ) -> dict[str, Any]:
        """Alias for ``get_orderbook``."""
        return await self.get_orderbook(market, limit=limit)

    async def get_best_bid_ask(
        self, symbol: str = "SOL_USDC"
    ) -> dict[str, float | None]:
        """Top of book using min(ask) / max(bid), not raw ``asks[0]``."""
        from src.cex.backpack_ticker import best_bid_ask_from_book

        book = await self.get_order_book(symbol, depth=20)
        top = best_bid_ask_from_book(book)
        if top is None:
            return {"best_bid": None, "best_ask": None}
        best_bid, best_ask = top
        return {"best_bid": best_bid, "best_ask": best_ask}

    async def get_bid_price(self, symbol: str = "SOL") -> float | None:
        """Best CEX bid (USDC per SOL) — use for DEX→CEX reverse sell leg."""
        sym = symbol.strip().upper().replace("/", "_")
        if sym == "SOL":
            sym = "SOL_USDC"
        elif not sym.endswith("_USDC") and "_" not in sym:
            sym = f"{sym}_USDC"
        top = await self.get_best_bid_ask(sym)
        bid = top.get("best_bid")
        return float(bid) if bid and bid > 0 else None

    async def get_best_ask(self, symbol: str = "SOL_USDC") -> float | None:
        """Ask price for CEX market buy modeling."""
        top = await self.get_best_bid_ask(symbol)
        ask = top.get("best_ask")
        if ask and ask > 0:
            logger.debug("Backpack best_ask for %s: %s", symbol, ask)
            return float(ask)
        return None

    async def get_best_bid(self, market: str = "SOL_USDC") -> float:
        """Best bid price from order book (strategy facade)."""
        bid = await self.get_bid_price(market)
        return float(bid or 0.0)

    async def get_ticker(self, market: str = "SOL_USDC") -> dict[str, Any]:
        return await self._public_get("/api/v1/ticker", {"symbol": _normalize_symbol(market)})

    async def get_market_mid_price(self, symbol: str = "SOL_USDC") -> float | None:
        from src.cex.backpack_ticker import mid_from_orderbook, mid_price_from_ticker

        sym = _normalize_symbol(symbol)
        try:
            data = await self._public_get("/api/v1/ticker", {"symbol": sym})
            mid = mid_price_from_ticker(data, symbol=sym)
            if mid and mid > 0:
                return mid
            book = await self.get_orderbook(sym, limit=5)
            return mid_from_orderbook(book)
        except Exception as exc:
            logger.error("Backpack price fetch failed %s: %s", sym, exc)
            return None

    async def get_sol_usdc_price(self) -> float | None:
        return await self.get_market_mid_price("SOL_USDC")

    async def get_market_filters(
        self, symbol: str = "SOL_USDC"
    ) -> tuple[Decimal, Decimal]:
        """Return (stepSize, minQuantity) for a Backpack spot symbol."""
        sym = _normalize_symbol(symbol)
        cached = self._market_filters_cache.get(sym)
        if cached:
            return cached["step"], cached["min"]

        step = _DEFAULT_SOL_QUANTITY_STEP
        minimum = _DEFAULT_SOL_MIN_QUANTITY
        try:
            raw = await self._public_get("/api/v1/markets")
            markets: list[Any]
            if isinstance(raw, list):
                markets = raw
            else:
                nested = raw.get("data")
                markets = nested if isinstance(nested, list) else []

            for market in markets:
                if not isinstance(market, dict):
                    continue
                if str(market.get("symbol", "")).upper() != sym:
                    continue
                filters = market.get("filters") or {}
                qty_filters = filters.get("quantity") or {}
                step_raw = qty_filters.get("stepSize")
                min_raw = qty_filters.get("minQuantity")
                if step_raw:
                    step = Decimal(str(step_raw))
                if min_raw:
                    minimum = Decimal(str(min_raw))
                break
        except Exception as exc:
            logger.warning(
                "Backpack market filters fetch failed for %s: %s (using defaults)",
                sym,
                exc,
            )

        self._market_filters_cache[sym] = {"step": step, "min": minimum}
        return step, minimum

    async def sanitize_order_quantity(
        self,
        qty: float,
        *,
        symbol: str = "SOL_USDC",
        side: str = "sell",
    ) -> tuple[float, str]:
        step, minimum = await self.get_market_filters(symbol)
        return sanitize_quantity(
            qty,
            step_size=step,
            min_qty=minimum,
            side=side,
        )

    async def sanitize_sol_quantity(
        self,
        qty: float,
        *,
        symbol: str = "SOL_USDC",
    ) -> float:
        """Round down to Backpack SOL/USDC ``stepSize`` (fixes INVALID_QUANTITY)."""
        safe, _qty_str = await self.sanitize_order_quantity(
            qty,
            symbol=symbol,
            side="sell",
        )
        return safe

    async def get_cex_buy_reference_price(
        self, symbol: str = "SOL_USDC"
    ) -> tuple[float | None, float | None, float | None]:
        from src.cex.backpack_ticker import mid_price_from_ticker

        sym = _normalize_symbol(symbol)
        use_ask = os.getenv("CEX_USE_ASK_FOR_BUY", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        mid = await self.get_market_mid_price(sym)
        ask = await self.get_best_ask(sym)
        buy: float | None = None

        if use_ask and ask and ask > 0:
            buy = ask
        elif use_ask:
            ticker = await self.get_ticker(sym)
            last = mid_price_from_ticker(ticker, symbol=sym)
            if last and last > 0:
                buy = last
                logger.debug("CEX buy reference ticker fallback for %s: %s", sym, last)

        if buy is None and mid and mid > 0:
            buy = mid

        if buy and buy > 0:
            return buy, mid, ask
        return None, mid, ask

    async def check_ask_depth_for_buy(
        self,
        usdc_amount_micro: int,
        *,
        symbol: str = "SOL_USDC",
        depth_mult: float | None = None,
        max_spread_bps: float | None = None,
    ) -> tuple[bool, str]:
        from src.cex.backpack_ticker import best_bid_ask_from_book, cumulative_ask_usdc

        if usdc_amount_micro <= 0:
            return False, "zero_size"

        sym = _normalize_symbol(symbol)
        mult = depth_mult if depth_mult is not None else float(
            os.getenv("CEX_MIN_BOOK_DEPTH_MULT", "1.5")
        )
        spread_cap = max_spread_bps if max_spread_bps is not None else float(
            os.getenv("CEX_MAX_BOOK_SPREAD_BPS", "50")
        )
        book = await self.get_orderbook(sym, limit=20)
        if not book.get("asks"):
            return False, "empty_asks"

        top = best_bid_ask_from_book(book)
        if top is not None:
            best_bid, best_ask = top
            if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
                spread_bps = (best_ask - best_bid) / best_ask * 10_000.0
                if spread_bps > spread_cap:
                    return False, f"wide_spread_{spread_bps:.1f}bps"

        required_usdc = (usdc_amount_micro / 1_000_000.0) * mult
        cumulative = cumulative_ask_usdc(book, max_levels=20)
        if cumulative >= required_usdc:
            return True, "ok"

        return False, f"thin_book_{cumulative:.2f}_need_{required_usdc:.2f}"

    async def check_ask_depth(
        self,
        *,
        symbol: str = "SOL",
        required_usdc: int,
        depth_mult: float | None = None,
    ) -> bool:
        """
        True when cumulative ask liquidity covers ``required_usdc`` (USDC micro).

        ``symbol`` may be ``SOL``, ``SOL/USDC``, or ``SOL_USDC``.
        """
        sym = symbol.strip().upper().replace("/", "_")
        if sym == "SOL":
            sym = "SOL_USDC"
        elif not sym.endswith("_USDC") and "_" not in sym:
            sym = f"{sym}_USDC"
        ok, _reason = await self.check_ask_depth_for_buy(
            int(required_usdc),
            symbol=sym,
            depth_mult=depth_mult,
        )
        return ok

    async def get_recent_prices(
        self,
        symbol: str = "SOL",
        *,
        minutes: float = 5,
    ) -> list[float]:
        """Rolling CEX buy-reference prices (``volatility_gate`` sample buffer)."""
        return await self.get_price_history(symbol, minutes=minutes)

    async def get_price_history(
        self,
        symbol: str = "SOL",
        *,
        minutes: float = 5,
    ) -> list[float]:
        """CEX buy-reference prices for the last ``minutes`` (records current ask)."""
        from src.strategies.volatility_gate import get_recent_prices, record_cex_price

        sym = symbol.strip().upper().replace("/", "_")
        if sym == "SOL":
            sym = "SOL_USDC"
        elif not sym.endswith("_USDC") and "_" not in sym:
            sym = f"{sym}_USDC"
        buy, _, _ = await self.get_cex_buy_reference_price(sym)
        if buy and buy > 0:
            record_cex_price(float(buy))
        return get_recent_prices(minutes=minutes)

    async def get_price(self, side: str, amount_usdc: int) -> float:
        _ = (side, amount_usdc)
        mid = await self.get_sol_usdc_price()
        return float(mid or 0.0)

    async def place_order(
        self,
        side: str,
        price_or_size: float,
        quantity: float | None = None,
        order_type: str = "MARKET",
        *,
        symbol: str = "SOL_USDC",
        quantity_str: str | None = None,
    ) -> dict[str, Any]:
        side_norm = side.strip().lower()
        backpack_side = "Bid" if side_norm in ("buy", "bid", "long") else "Ask"
        otype = "Market" if order_type.upper() == "MARKET" else "Limit"
        sym = _normalize_symbol(symbol)

        if self.settings.test_mode or self.settings.simulate:
            logger.info(
                "SIMULATE %s %s %s @ %s",
                backpack_side,
                quantity or price_or_size,
                sym,
                price_or_size if otype == "Limit" else "market",
            )
            return {"success": True, "orderId": "sim-123", "status": "Filled"}

        if quantity is None:
            quote_usdc = float(price_or_size)
            if quote_usdc > 1_000_000:
                quote_usdc /= 1_000_000.0
            # Backpack rejects quoteQuantity with more than 2 decimal places
            quote_usdc_str = f"{quote_usdc:.2f}"
            body: dict[str, Any] = {
                "symbol": sym,
                "side": backpack_side,
                "orderType": "Market",
                "quoteQuantity": quote_usdc_str,
                "timeInForce": "IOC",
            }
        else:
            if quantity_str is not None:
                qty_f = float(quantity)
                qty_str = quantity_str
            else:
                qty_f, qty_str = await self.sanitize_order_quantity(
                    float(quantity),
                    symbol=sym,
                    side=side_norm,
                )
            if qty_f <= 0:
                return {
                    "success": False,
                    "error": "quantity_too_small_after_sanitize",
                    "symbol": sym,
                }
            if otype == "Market":
                body = {
                    "symbol": sym,
                    "side": backpack_side,
                    "orderType": "Market",
                    "quantity": qty_str,
                    "timeInForce": "IOC",
                }
            else:
                body = {
                    "symbol": sym,
                    "side": backpack_side,
                    "orderType": otype,
                    "price": str(price_or_size),
                    "quantity": qty_str,
                    "timeInForce": "GTC",
                }

        result = await self._signed_request(
            "POST",
            "/api/v1/order",
            "orderExecute",
            body=body,
        )
        if result.get("success"):
            return result
        return {
            "success": False,
            "error": str(result.get("error") or result.get("message") or "order_failed"),
            "symbol": sym,
        }

    async def place_market_buy(
        self, symbol: str, size_usdc_micro: int
    ) -> dict[str, Any]:
        """Market buy spending ``size_usdc_micro`` USDC (6 decimals)."""
        return await self.place_order("buy", float(size_usdc_micro), symbol=symbol)

    async def place_sell_order(
        self,
        sol_amount: float,
        *,
        symbol: str = "SOL_USDC",
        price: float | None = None,
    ) -> dict[str, Any]:
        """Sanitized Backpack market sell (``price`` is bid hint for sizing/logging only)."""
        executor = BackpackExecutor(self)
        return await executor.sell_sol(
            float(sol_amount),
            price=price,
            symbol=symbol,
        )

    async def sell_sol(
        self,
        sol_amount: float,
        price: float | None = None,
        *,
        symbol: str = "SOL_USDC",
    ) -> dict[str, Any]:
        """Alias for :meth:`BackpackExecutor.sell_sol`."""
        executor = BackpackExecutor(self)
        return await executor.sell_sol(sol_amount, price=price, symbol=symbol)

    async def execute_market_sell(
        self,
        symbol: str,
        amount_sol: float,
        *,
        price_hint: float | None = None,
    ) -> dict[str, Any]:
        """Market sell ``amount_sol`` base on Backpack (IOC, quantity-only)."""
        sym = _normalize_symbol(symbol)
        if amount_sol <= 0:
            return {"success": False, "error": "zero_amount"}

        if self.settings.test_mode or self.settings.simulate:
            logger.info("SIMULATE market sell %.6f SOL %s", amount_sol, sym)
            return {
                "success": True,
                "status": "Filled",
                "filled_sol": amount_sol,
            }

        bid = float(price_hint or 0)
        if bid <= 0:
            bid_ask = await self.get_best_bid_ask(sym)
            bid = float(bid_ask.get("best_bid") or 0)
        if bid <= 0:
            bid = float(await self.get_market_mid_price(sym) or 0)
        if bid <= 0:
            cached = self._ws_ticker_cache.get(sym) or {}
            for key in ("best_bid", "bid", "last", "lastPrice"):
                try:
                    px = float(cached.get(key) or 0)
                    if px > 0:
                        bid = px
                        break
                except (TypeError, ValueError):
                    continue
        if bid <= 0:
            live_bid = await self.get_bid_price(sym.replace("_USDC", ""))
            bid = float(live_bid or 0)

        if bid <= 0:
            return {"success": False, "error": "no_bid_price"}

        step, min_qty = await self.get_market_filters(sym)
        retry_steps: list[Decimal] = [step]
        coarse = step * Decimal("10")
        if coarse > step:
            retry_steps.append(coarse)
        if Decimal("1") not in retry_steps:
            retry_steps.append(Decimal("1"))

        last_error = "order_failed"
        for step_idx, try_step in enumerate(retry_steps):
            sell_qty, qty_str = sanitize_quantity(
                float(amount_sol),
                step_size=try_step,
                min_qty=min_qty,
                side="sell",
            )
            if sell_qty <= 0:
                last_error = "quantity_too_small_after_sanitize"
                continue
            if step_idx > 0:
                logger.error(
                    "Backpack precision error - retrying with stricter round | step=%s qty=%s",
                    try_step,
                    qty_str,
                )

            for attempt in range(3):
                result = await self.place_order(
                    "sell",
                    bid,
                    quantity=sell_qty,
                    order_type="MARKET",
                    symbol=sym,
                    quantity_str=qty_str,
                )
                if result.get("success"):
                    result["filled_sol"] = sell_qty
                    if bid > 0:
                        result["usdc_received"] = sell_qty * bid
                    logger.info(
                        "CEX market sell success | qty=%s market=%s bid=%.4f",
                        qty_str,
                        sym,
                        bid,
                    )
                    return result
                last_error = str(
                    result.get("error") or result.get("message") or "order_failed"
                )
                if not is_invalid_quantity_error(last_error):
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                break

            if not is_invalid_quantity_error(last_error):
                break

        return {"success": False, "error": last_error or "no_bid_price"}

    async def execute_market_buy(
        self,
        usdc_amount_micro: int,
        *,
        symbol: str = "SOL_USDC",
    ) -> bool:
        result = await self.place_order("buy", float(usdc_amount_micro), symbol=symbol)
        if not result.get("success"):
            return False
        status = str(result.get("status", "Filled")).lower()
        return status in ("filled", "partially_filled", "new", "open") or result.get("id") is not None

    async def withdraw_token(
        self,
        mint: str,
        amount: int,
        *,
        destination: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """
        Withdraw SPL token to on-chain wallet (Backpack capital API).

        Future: map ``mint`` → Backpack symbol and call signed withdraw.
        """
        _ = (mint, amount, destination, symbol)
        logger.warning(
            "withdraw_token not implemented | mint=%s amount=%s (use withdraw_sol for SOL)",
            mint,
            amount,
        )
        return {"success": False, "error": "withdraw_token_not_implemented"}

    def clear_balance_cache(self, asset: str | None = None) -> None:
        """Invalidate cached CEX balances after deposits/withdrawals."""
        if asset:
            self._balance_cache.pop(str(asset).upper(), None)
        else:
            self._balance_cache.clear()

    async def get_deposit_address(
        self, blockchain: str = "Solana"
    ) -> dict[str, Any]:
        """Backpack user deposit address for on-chain transfers (signed API)."""
        if self.settings.test_mode or self.settings.simulate:
            return {
                "success": True,
                "address": "SimulatedBackpackDepositAddress1111111111111111111",
                "blockchain": blockchain,
            }
        result = await self._signed_request(
            "GET",
            "/wapi/v1/capital/deposit/address",
            "depositAddressQuery",
            params={"blockchain": blockchain},
        )
        if not result.get("success"):
            return result
        address = str(result.get("address") or "").strip()
        if not address:
            return {"success": False, "error": "deposit_address_empty", "raw": result}
        return {"success": True, "address": address, "blockchain": blockchain}

    async def withdraw_sol(self, amount_sol: float, destination: str) -> dict[str, Any]:
        """Withdraw SOL to on-chain address (signed Backpack API)."""
        if self.settings.test_mode or self.settings.simulate:
            logger.info("TEST: Would withdraw %.6f SOL to %s", amount_sol, destination)
            return {"success": True, "status": "ok"}

        body = {
            "address": destination,
            "quantity": str(amount_sol),
            "symbol": "SOL",
        }
        result = await self._signed_request(
            "POST",
            "/api/v1/capital/withdraw",
            "withdraw",
            body=body,
        )
        if result:
            result.setdefault("success", True)
        return result

    async def withdraw_usdc(
        self, amount_usdc: float, destination: str, *, blockchain: str = "Solana"
    ) -> dict[str, Any]:
        """Withdraw USDC to on-chain SPL wallet (Backpack capital API)."""
        if self.settings.test_mode or self.settings.simulate:
            logger.info(
                "TEST: Would withdraw %.2f USDC (%s) to %s",
                amount_usdc,
                blockchain,
                destination,
            )
            return {"success": True, "status": "ok"}

        qty = f"{float(amount_usdc):.2f}".rstrip("0").rstrip(".")
        primary_body = {
            "address": destination,
            "quantity": qty,
            "symbol": "USDC",
        }
        result = await self._signed_request(
            "POST",
            "/api/v1/capital/withdraw",
            "withdraw",
            body=primary_body,
        )
        if result.get("success"):
            return result

        legacy_body = {
            "address": destination,
            "blockchain": blockchain,
            "quantity": qty,
            "symbol": "USDC",
        }
        legacy = await self._signed_request(
            "POST",
            "/wapi/v1/capital/withdrawals",
            "withdraw",
            body=legacy_body,
        )
        if legacy.get("success"):
            return legacy

        err = str(
            legacy.get("error")
            or result.get("error")
            or "withdraw_usdc_failed"
        )
        logger.error(
            "USDC withdraw failed | amount=%s dest=%s err=%s",
            qty,
            destination[:12],
            err[:200],
        )
        return {"success": False, "error": err, "primary": result, "legacy": legacy}

    async def withdraw_sol_to_chain(
        self, amount_sol: float, destination: str
    ) -> str | None:
        """Withdraw SOL; return tx id / reference when present."""
        result = await self.withdraw_sol(amount_sol, destination)
        if not result or not result.get("success", bool(result)):
            return None
        for key in ("id", "withdrawalId", "txid", "txId", "transactionId"):
            if result.get(key):
                return str(result[key])
        return "ok"

    async def execute_cex_buy_then_withdraw(
        self,
        size_usdc_micro: int,
        pair: str = "SOL_USDC",
        *,
        destination: str | None = None,
    ) -> str | None:
        """CEX market buy → withdraw SOL to Ledger wallet."""
        sym = _normalize_symbol(pair)
        order = await self.place_market_buy(sym, size_usdc_micro)
        if not order or not order.get("success"):
            return None

        await asyncio.sleep(8)

        cex_price = await self.get_best_ask(sym) or await self.get_market_mid_price(sym)
        usdc = size_usdc_micro / 1_000_000.0
        fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
        filled_sol = usdc / max(float(cex_price or 1.0), 1e-9) * fudge

        dest = (destination or self.settings.WALLET_PUBKEY or "").strip()
        if not dest:
            logger.error("WALLET_PUBKEY not set — cannot withdraw")
            return None

        txid = await self.withdraw_sol_to_chain(filled_sol, dest)
        if txid:
            logger.info(
                "cex_withdraw_initiated txid=%s size_usdc=%.2f filled_sol=%.6f",
                txid,
                usdc,
                filled_sol,
            )
        buffer_sec = float(
            getattr(self.settings, "CEX_WITHDRAWAL_BUFFER_SEC", None)
            or os.getenv("CEX_WITHDRAWAL_BUFFER_SEC", "22")
        )
        await asyncio.sleep(buffer_sec)
        return txid

    async def sufficient_usdc_for_buy(
        self,
        usdc_amount_micro: int,
        *,
        buffer_usdc: float | None = None,
    ) -> tuple[bool, float, float]:
        required = float(usdc_amount_micro) / 1_000_000.0
        if buffer_usdc is None:
            buffer_usdc = float(os.getenv("CEX_BUY_BALANCE_BUFFER_USDC", "0.25"))
        available = await self.get_balance("USDC")
        ok = available + 1e-9 >= required + float(buffer_usdc)
        return ok, available, required

    async def get_balances(self) -> dict[str, Any]:
        if self.settings.test_mode or self.settings.simulate:
            return {"USDC": {"available": "100000"}, "SOL": {"available": "10"}}
        return await self._signed_request("GET", "/api/v1/capital", "balanceQuery")

    async def get_balance(self, asset: str = "USDC", *, force_refresh: bool = False) -> float:
        now = time.monotonic()
        if not force_refresh and asset in self._balance_cache:
            cached_val, cached_at = self._balance_cache[asset]
            if now - cached_at < self._balance_cache_ttl:
                return cached_val

        value = await self._fetch_balance_uncached(asset)
        self._balance_cache[asset] = (value, now)
        return value

    async def _fetch_balance_uncached(self, asset: str) -> float:
        try:
            data = await self.get_balances()
            if isinstance(data, list):
                for item in data:
                    if str(item.get("asset", "")).upper() == asset.upper():
                        return float(item.get("available", 0) or 0)
            if isinstance(data, dict):
                if asset.upper() in data:
                    entry = data[asset.upper()]
                    if isinstance(entry, dict):
                        return float(entry.get("available", 0) or 0)
                    return float(entry)
                for key in ("balances", "data", "result"):
                    nested = data.get(key)
                    if isinstance(nested, list):
                        for item in nested:
                            if str(item.get("asset", "")).upper() == asset.upper():
                                return float(item.get("available", 0) or 0)
            return 0.0
        except Exception as exc:
            logger.debug("get_balance(%s) failed: %s", asset, exc)
            return 0.0

    def _parse_ws_ticker_frame(self, raw: str) -> dict[str, Any] | None:
        """Parse Backpack WS envelope ``{stream, data}`` into a normalized ticker update."""
        from src.cex.backpack_ticker import mid_price_from_ticker, parse_json_body

        obj = parse_json_body(raw)
        if not isinstance(obj, dict):
            return None

        stream = str(obj.get("stream") or "")
        data = obj.get("data")
        if isinstance(data, str):
            inner = parse_json_body(data)
            data = inner if isinstance(inner, dict) else obj
        if not isinstance(data, dict):
            return None

        symbol = _normalize_symbol(str(data.get("s") or data.get("symbol") or ""))
        if not symbol and "." in stream:
            symbol = _normalize_symbol(stream.split(".", 1)[-1])

        out: dict[str, Any] = {
            "stream": stream,
            "symbol": symbol,
            "data": data,
        }

        event = str(data.get("e") or "")
        if event == "bookTicker":
            try:
                bid = float(data.get("b") or 0)
                ask = float(data.get("a") or 0)
                if bid > 0 and ask > 0:
                    out["best_bid"] = bid
                    out["best_ask"] = ask
                    out["mid"] = (bid + ask) / 2.0
            except (TypeError, ValueError):
                pass
        elif event == "ticker":
            mid = mid_price_from_ticker(data, symbol=symbol or None)
            if mid and mid > 0:
                out["mid"] = mid
                bid = data.get("bidPrice") or data.get("bestBid")
                ask = data.get("askPrice") or data.get("bestAsk")
                try:
                    if bid is not None and ask is not None:
                        b, a = float(bid), float(ask)
                        if b > 0 and a > 0:
                            out["best_bid"] = b
                            out["best_ask"] = a
                except (TypeError, ValueError):
                    pass

        return out if symbol or stream else None

    def get_ws_ticker_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Last WS update for a symbol (after ``subscribe_ticker`` / background task)."""
        return self._ws_ticker_cache.get(_normalize_symbol(symbol))

    async def subscribe_ticker(
        self,
        symbols: list[str] | str,
        *,
        include_book_ticker: bool = True,
        include_ticker: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribe to Backpack ticker streams and yield parsed updates.

        Connects to ``wss://ws.backpack.exchange`` (override with ``BACKPACK_WS_URL``).
        Sends ``{"method":"SUBSCRIBE","params":["ticker.SOL_USDC",...]}``.

        Yields dicts with ``stream``, ``symbol``, ``data``, and optional
        ``best_bid``, ``best_ask``, ``mid``.
        """
        import websockets

        if isinstance(symbols, str):
            sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        else:
            sym_list = list(symbols)

        params = _ticker_stream_params(
            sym_list,
            include_book_ticker=include_book_ticker,
            include_ticker=include_ticker,
        )
        if not params:
            return

        ws_url = _ws_url()
        ping_interval = float(os.getenv("BACKPACK_WS_PING_INTERVAL_SEC", "20"))
        ping_timeout = float(os.getenv("BACKPACK_WS_PING_TIMEOUT_SEC", "60"))

        async with websockets.connect(
            ws_url,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            await ws.send(
                json.dumps({"method": "SUBSCRIBE", "params": params})
            )
            logger.info("Backpack WS subscribed | url=%s streams=%s", ws_url, params)

            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        text = raw.decode("utf-8", errors="replace")
                    else:
                        text = str(raw)

                    if text.upper() == "PING":
                        await ws.send("PONG")
                        continue

                    msg = self._parse_ws_ticker_frame(text)
                    if not msg:
                        continue

                    sym = str(msg.get("symbol") or "")
                    if sym:
                        self._ws_ticker_cache[sym] = msg
                    yield msg
            finally:
                if self._ws is ws:
                    self._ws = None
                try:
                    await ws.send(
                        json.dumps({"method": "UNSUBSCRIBE", "params": params})
                    )
                except Exception:
                    pass

    async def run_ticker_ws(
        self,
        symbols: list[str] | str,
        *,
        on_update: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        """Background-friendly loop: reconnect on disconnect."""
        backoff = 2.0
        while True:
            try:
                async for msg in self.subscribe_ticker(symbols):
                    if on_update is not None:
                        maybe = on_update(msg)
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Backpack WS ticker disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class BackpackExecutor:
    """
    SOL/USDC sell helper with quantity sanitization.

    Live orders use :class:`BackpackClient` (ED25519 signed REST). CCXT is only
    used optionally to cross-check market step metadata.
    """

    def __init__(
        self,
        client: BackpackClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client or BackpackClient(settings)
        self._market: dict[str, Any] | None = None

    async def ensure_market(self, symbol: str = "SOL_USDC") -> dict[str, Any]:
        sym = _normalize_symbol(symbol)
        if self._market and self._market.get("symbol") == sym:
            return self._market

        step, minimum = await self._client.get_market_filters(sym)
        market: dict[str, Any] = {
            "symbol": sym,
            "step_size": step,
            "min_quantity": minimum,
            "precision": {"amount": _decimals_from_step(step)},
            "limits": {"amount": {"step": str(step), "min": str(minimum)}},
        }

        try:
            from src.cex.ccxt_wrapper import create_backpack_exchange

            exchange = create_backpack_exchange()
            await asyncio.to_thread(exchange.load_markets)
            ccxt_market = exchange.market("SOL/USDC")
            ccxt_prec = ccxt_market.get("precision", {}).get("amount")
            ccxt_step = (
                ccxt_market.get("limits", {}).get("amount", {}).get("min")
            )
            if ccxt_prec is not None:
                market["precision"]["amount"] = ccxt_prec
            if ccxt_step:
                market["limits"]["amount"]["step"] = str(ccxt_step)
                try:
                    market["step_size"] = Decimal(str(ccxt_step))
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("CCXT market metadata skipped: %s", exc)

        self._market = market
        return market

    async def sanitize_sol_quantity(
        self,
        qty: float,
        *,
        symbol: str = "SOL_USDC",
    ) -> float:
        """Critical fix: round DOWN to Backpack SOL/USDC step (avoids INVALID_QUANTITY)."""
        market = await self.ensure_market(symbol)
        step = market["step_size"]
        minimum = market["min_quantity"]
        safe, qty_str = sanitize_quantity(
            float(qty),
            step_size=step,
            min_qty=minimum,
            side="sell",
        )
        if safe <= 0:
            logger.warning(
                "Quantity too small after sanitize: %s (original: %s step=%s)",
                safe,
                qty,
                step,
            )
            return 0.0

        logger.info(
            "Sanitized SOL qty: %.8f -> %s (step=%s)",
            qty,
            qty_str,
            step,
        )
        return safe

    async def sell_sol(
        self,
        sol_amount: float,
        price: float | None = None,
        *,
        symbol: str = "SOL_USDC",
    ) -> dict[str, Any]:
        sym = _normalize_symbol(symbol)
        safe_qty = await self.sanitize_sol_quantity(sol_amount, symbol=sym)
        if safe_qty <= 0:
            return {"success": False, "error": "quantity_too_small_after_sanitize"}

        result = await self._client.execute_market_sell(
            sym,
            safe_qty,
            price_hint=price,
        )
        if result.get("success"):
            order_id = result.get("orderId") or result.get("id")
            logger.info(
                "Backpack SELL SUCCESS | qty=%.8f | order=%s",
                safe_qty,
                order_id,
            )
            return result

        err = str(result.get("error") or "")
        if is_invalid_quantity_error(err):
            logger.error("Backpack precision retry with stricter round")
            market = await self.ensure_market(sym)
            strict_step = market["step_size"] * Decimal("10")
            strict_qty, _strict_str = sanitize_quantity(
                float(sol_amount),
                step_size=strict_step,
                min_qty=market["min_quantity"],
                side="sell",
            )
            if strict_qty > 0:
                return await self._client.execute_market_sell(
                    sym,
                    strict_qty,
                    price_hint=price,
                )
        return result


_backpack_client: BackpackClient | None = None
_backpack_executor: BackpackExecutor | None = None


def get_backpack_client(settings: Settings | None = None) -> BackpackClient:
    global _backpack_client
    if _backpack_client is None:
        _backpack_client = BackpackClient(settings)
    return _backpack_client


def get_backpack_executor(settings: Settings | None = None) -> BackpackExecutor:
    global _backpack_executor
    if _backpack_executor is None:
        _backpack_executor = BackpackExecutor(get_backpack_client(settings))
    return _backpack_executor
