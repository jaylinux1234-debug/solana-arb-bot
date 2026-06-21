"""Token discovery sources for meme sniping (pump.fun + DexScreener fallback)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PUMP_URLS = (
    "https://frontend-api-v3.pump.fun/coins/currently-live?limit=30",
    "https://frontend-api.pump.fun/coins?offset=0&limit=30&sort=created_timestamp&order=DESC&includeNsfw=false",
    "https://client-api-2.pump.fun/coins?limit=30",
)
_DEX_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
_DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"

_pump_fail_streak = 0
_last_pump_warn = 0.0
_dex_pair_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 45.0


def _pump_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://pump.fun",
        "Referer": "https://pump.fun/",
    }


def _normalize_pump_coin(coin: dict[str, Any]) -> dict[str, Any] | None:
    mint = str(coin.get("mint") or coin.get("token") or "").strip()
    if not mint:
        return None
    return {
        "mint": mint,
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "liquidity": float(
            coin.get("usd_market_cap") or coin.get("liquidity") or coin.get("market_cap") or 0
        ),
        "market_cap": float(coin.get("usd_market_cap") or coin.get("market_cap") or 0),
        "price_change_5m": coin.get("price_change_5m", 0),
        "dev_percentage": coin.get("dev_percentage", 0),
        "social_mentions": coin.get("reply_count") or coin.get("social_mentions") or 0,
        "volatility_bps": coin.get("volatility_bps", 0),
        "source": "pump.fun",
    }


async def _fetch_pump_fun(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    global _pump_fail_streak, _last_pump_warn

    proxy = (os.getenv("MEME_SNIPING_HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
    last_status = 0

    for url in _PUMP_URLS:
        try:
            resp = await client.get(url, headers=_pump_headers())
            last_status = resp.status_code
            if resp.status_code != 200:
                continue
            payload = resp.json()
            coins = payload if isinstance(payload, list) else payload.get("coins", [])
            out: list[dict[str, Any]] = []
            for coin in coins:
                if not isinstance(coin, dict):
                    continue
                normalized = _normalize_pump_coin(coin)
                if normalized:
                    out.append(normalized)
            if out:
                _pump_fail_streak = 0
                return out
        except Exception as exc:
            logger.debug("pump.fun fetch failed | url=%s err=%s", url[:48], exc)

    _pump_fail_streak += 1
    now = time.monotonic()
    if _pump_fail_streak >= 5 and now - _last_pump_warn > 120:
        _last_pump_warn = now
        logger.warning(
            "pump.fun API unavailable (last HTTP %s, streak=%d) — using DexScreener fallback",
            last_status,
            _pump_fail_streak,
        )
    return []


def _social_score(profile: dict[str, Any], pair: dict[str, Any] | None) -> int:
    score = 0
    links = profile.get("links") or []
    score += min(30, len(links) * 12)
    if profile.get("description"):
        score += 8
    if pair:
        info = pair.get("info") or {}
        socials = info.get("socials") or []
        score += min(35, len(socials) * 15)
        txns = pair.get("txns") or {}
        m5 = txns.get("m5") or {}
        score += min(25, int(m5.get("buys") or 0))
    return score


def _volatility_bps(pair: dict[str, Any] | None) -> int:
    if not pair:
        return 0
    pc = pair.get("priceChange") or {}
    for key in ("m5", "h1", "h6"):
        raw = pc.get(key)
        if raw is not None:
            try:
                return int(abs(float(raw)) * 100)
            except (TypeError, ValueError):
                continue
    return 0


async def _fetch_dex_pair(client: httpx.AsyncClient, mint: str) -> dict[str, Any] | None:
    cached = _dex_pair_cache.get(mint)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]

    resp = await client.get(_DEX_TOKEN_URL.format(mint=mint), timeout=8.0)
    if resp.status_code != 200:
        return None
    pairs = resp.json().get("pairs") or []
    best: dict[str, Any] | None = None
    best_liq = 0.0
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "").lower() != "solana":
            continue
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        if liq >= best_liq:
            best_liq = liq
            best = pair
    if best:
        _dex_pair_cache[mint] = (now, best)
    return best


async def fetch_token_mark_price_usd(mint: str) -> float | None:
    """Best-effort USD mark from DexScreener (lower noise than Jupiter on thin pools)."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        pair = await _fetch_dex_pair(client, mint)
        if not pair:
            return None
        try:
            price = float(pair.get("priceUsd") or 0)
            return price if price > 0 else None
        except (TypeError, ValueError):
            return None


async def _profile_to_coin(client: httpx.AsyncClient, profile: dict[str, Any]) -> dict[str, Any] | None:
    mint = str(profile.get("tokenAddress") or "").strip()
    if not mint:
        return None
    pair = await _fetch_dex_pair(client, mint)
    liq = float((pair or {}).get("liquidity", {}).get("usd") or 0)
    base = (pair or {}).get("baseToken") or {}
    return {
        "mint": mint,
        "name": base.get("name") or str(profile.get("description") or "")[:40],
        "symbol": base.get("symbol"),
        "liquidity": liq,
        "market_cap": float((pair or {}).get("marketCap") or (pair or {}).get("fdv") or 0),
        "price_change_5m": (pair or {}).get("priceChange", {}).get("m5", 0),
        "dev_percentage": 0,
        "social_mentions": len(profile.get("links") or []),
        "volatility_bps": _volatility_bps(pair),
        "social_score": _social_score(profile, pair),
        "txns_m5_buys": int(((pair or {}).get("txns") or {}).get("m5", {}).get("buys") or 0),
        "source": "dexscreener",
    }


async def _fetch_dexscreener(client: httpx.AsyncClient, limit: int = 20) -> list[dict[str, Any]]:
    profiles_resp = await client.get(_DEX_PROFILES_URL, timeout=8.0)
    boosts_resp = await client.get(_DEX_BOOSTS_URL, timeout=8.0)

    profiles: list[dict[str, Any]] = []
    if profiles_resp.status_code == 200 and isinstance(profiles_resp.json(), list):
        profiles = profiles_resp.json()
    boosts: list[dict[str, Any]] = []
    if boosts_resp.status_code == 200 and isinstance(boosts_resp.json(), list):
        boosts = boosts_resp.json()

    if not profiles and not boosts:
        logger.warning(
            "DexScreener unavailable | profiles=%s boosts=%s",
            profiles_resp.status_code,
            boosts_resp.status_code,
        )
        return []

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for profile in (boosts + profiles)[: max(limit, 20)]:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("chainId") or "").lower() != "solana":
            continue
        mint = str(profile.get("tokenAddress") or "").strip()
        if not mint or mint in seen:
            continue
        seen.add(mint)
        coin = await _profile_to_coin(client, profile)
        if coin:
            merged.append(coin)
    merged.sort(key=lambda c: float(c.get("liquidity") or 0), reverse=True)
    return merged


async def fetch_candidate_coins(limit: int = 15) -> tuple[list[dict[str, Any]], str]:
    """Return normalized coin dicts and the source label used."""
    proxy = (os.getenv("MEME_SNIPING_HTTP_PROXY") or os.getenv("HTTPS_PROXY") or "").strip()
    client_kwargs: dict[str, Any] = {"timeout": 8.0}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        pump = await _fetch_pump_fun(client)
        if pump:
            return pump[:limit], "pump.fun"

        dex = await _fetch_dexscreener(client, limit=max(limit, 20))
        return dex[:limit], "dexscreener"
