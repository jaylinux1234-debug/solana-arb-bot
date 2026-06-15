import asyncio
import json
import logging
import os
import time

import aiohttp

logger = logging.getLogger(__name__)

_quote_lock = asyncio.Lock()
_last_quote_ts = 0.0
_quote_sem: asyncio.Semaphore | None = None


def _quote_semaphore() -> asyncio.Semaphore:
    global _quote_sem
    if _quote_sem is None:
        n = max(1, int(os.getenv("JUPITER_QUOTE_MAX_CONCURRENT", "2")))
        _quote_sem = asyncio.Semaphore(n)
    return _quote_sem


async def _throttle_jupiter_quote() -> None:
    global _last_quote_ts
    interval = max(0.05, float(os.getenv("JUPITER_QUOTE_MIN_INTERVAL_SEC", "0.35")))
    async with _quote_lock:
        now = time.monotonic()
        wait = interval - (now - _last_quote_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_quote_ts = time.monotonic()


async def get_jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = 50,
    platform_fee_bps: int | None = None,
    *,
    dexes: str | None = None,
) -> dict | None:
    """
    Best-effort Jupiter quote with API key, lite/metis URL from env, throttle, and 429 retries.
    """
    quote_url = (os.getenv("JUPITER_QUOTE_URL") or "https://lite-api.jup.ag/swap/v1/quote").strip()
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
    }
    if platform_fee_bps:
        params["platformFeeBps"] = str(platform_fee_bps)
    if dexes:
        params["dexes"] = dexes.strip()

    headers: dict[str, str] = {"Accept": "application/json"}
    key = (os.getenv("JUPITER_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key

    retries = max(1, int(os.getenv("JUPITER_QUOTE_MAX_RETRIES", "6")))
    base_delay = float(os.getenv("JUPITER_QUOTE_RETRY_DELAY_SEC", "0.8"))

    async with _quote_semaphore():
        await _throttle_jupiter_quote()
        raw_last = ""
        async with aiohttp.ClientSession() as session:
            for attempt in range(retries):
                try:
                    async with session.get(
                        quote_url,
                        params=params,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=25),
                    ) as resp:
                        raw_last = await resp.text()
                        if resp.status == 429 or resp.status in (500, 502, 503, 504):
                            await asyncio.sleep(base_delay * (2**attempt))
                            continue
                        if resp.status != 200:
                            logger.warning(
                                "Jupiter quote HTTP %s: %s",
                                resp.status,
                                raw_last[:200],
                            )
                            await asyncio.sleep(base_delay * (2**attempt))
                            continue
                        try:
                            data = json.loads(raw_last) if raw_last.strip() else {}
                        except json.JSONDecodeError:
                            low = raw_last.lower()
                            if "rate limit" in low or "too many requests" in low:
                                await asyncio.sleep(base_delay * (2**attempt))
                                continue
                            logger.warning("Jupiter quote not JSON: %s", raw_last[:200])
                            return None
                        if not isinstance(data, dict):
                            return None
                        if data.get("error"):
                            err = data.get("error")
                            if isinstance(err, str) and (
                                "rate limit" in err.lower() or "429" in err
                            ):
                                await asyncio.sleep(base_delay * (2**attempt))
                                continue
                            logger.warning("Jupiter quote error: %s", err)
                            return None
                        if "outAmount" not in data:
                            logger.warning("Jupiter quote missing outAmount")
                            return None
                        return data
                except TimeoutError:
                    logger.warning("Jupiter quote request timed out")
                    await asyncio.sleep(base_delay * (2**attempt))
                except Exception as e:
                    logger.warning("Jupiter quote error: %s", e)
                    await asyncio.sleep(base_delay * (2**attempt))

        logger.warning(
            "Jupiter quote retries exhausted (last HTTP body: %s)",
            raw_last[:200] if raw_last else "(empty)",
        )
        return None
