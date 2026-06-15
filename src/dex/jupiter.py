# src/dex/jupiter.py
"""
Jupiter Aggregator v6 — quotes, swap tx build, and Jito bundle execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import base64

import httpx
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from src.config.settings import Settings, get_settings
from src.dex.jupiter_params import (
    build_quote_query_params,
    build_swap_request_body,
    resolve_slippage_bps,
)
from src.dex.quote import _quote_semaphore, _throttle_jupiter_quote


def resolve_execution_jito_tip_lamports(
    net_bps: float | None = None,
    *,
    base_tip: int | None = None,
    size_usdc_micro: int | None = None,
    gross_bps: float | None = None,
    confidence: float | None = None,
    override_net_usd: float | None = None,
) -> int:
    """Jito tip via ``calculate_optimal_tip`` (profit-ratio + live floor + confidence)."""
    if net_bps is not None and _env_bool("JITO_DYNAMIC_TIP", True):
        from src.core.jito_tip import (
            calculate_optimal_tip,
            get_cached_tip_floor,
            log_jito_tip,
            mev_protection_enabled,
            modeled_net_usd,
        )

        if mev_protection_enabled():
            micro = size_usdc_micro
            if micro is None:
                micro = int(os.getenv("CEX_DEX_MAX_TRADE_USDC_MICRO", "12000000"))
            gross = float(gross_bps if gross_bps is not None else net_bps or 0.0)
            expected_net = (
                float(override_net_usd)
                if override_net_usd is not None
                else modeled_net_usd(float(net_bps), int(micro))
            )
            if expected_net <= 0:
                expected_net = float(os.getenv("JITO_TIP_FALLBACK_NET_USD", "8.0"))
            tip = calculate_optimal_tip(
                expected_net,
                gross,
                confidence=confidence,
                tip_floor=get_cached_tip_floor(),
            )
            log_jito_tip(tip, expected_net)
            return tip

    tip = int(
        base_tip
        if base_tip is not None
        else os.getenv("JITO_TIP_LAMPORTS", "100000")
    )
    if net_bps is not None and float(net_bps) > 15:
        cap = int(os.getenv("JITO_TIP_MAX_LAMPORTS", os.getenv("JITO_TIP_LAMPORTS_MAX", "180000")))
        tip = min(cap, tip * 2)
    return tip


_JITO_TIP_ACCOUNTS: tuple[str, ...] = (
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4bVZkVjssHtCYJFaq7",
    "Cw8CFyM9FkoMi7K7Crf6HNasqf3AQNoenzzcsc1Uw4m",
    "ADaUMid9yfUytqMBgopwji2MbR7UxcE5vtpkBqJpCcT",
    "DttWaMuVvTiduZRnguLF7jBSE1bGUx3kQua49",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcWjWTfrTQg",
    "DfXygSm4jCyNCybV3Kcc3Sa7dBrXYnbhzRV29qhH",
)


def _resolve_jito_tip_account() -> str:
    custom = (os.getenv("JITO_TIP_ACCOUNT") or "").strip()
    if custom:
        return custom
    return random.choice(_JITO_TIP_ACCOUNTS)


async def build_signed_jito_tip_b64(keypair: Any, tip_lamports: int) -> str | None:
    """Build a signed SOL transfer to a Jito tip account (bundle landing incentive)."""
    if keypair is None or int(tip_lamports) <= 0:
        return None
    try:
        from solana.rpc.async_api import AsyncClient
        from solders.message import MessageV0
        from solders.system_program import TransferParams, transfer

        from src.core.rpc_config import call_with_rpc_fallback

        async def _blockhash(rpc: str):
            async with AsyncClient(rpc) as client:
                resp = await client.get_latest_blockhash()
                return resp.value.blockhash

        blockhash = await call_with_rpc_fallback(
            "transaction",
            _blockhash,
            label="jito_tip_blockhash",
        )
        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=Pubkey.from_string(_resolve_jito_tip_account()),
                lamports=int(tip_lamports),
            )
        )
        msg = MessageV0.try_compile(keypair.pubkey(), [ix], [], blockhash)
        signed = VersionedTransaction(msg, [keypair])
        return base64.b64encode(bytes(signed)).decode()
    except Exception as exc:
        logger.warning("Jito tip tx build failed: %s", exc)
        return None


async def _send_signed_jito_bundle(
    signed_b64: str,
    *,
    tip_lamports: int,
    tip_b64: str | None = None,
) -> dict[str, Any]:
    """Submit signed swap tx via Jito (multi-region + optional landing poll)."""
    from src.execution.jito_bundle import get_jito_bundle_executor

    txs = [signed_b64]
    if tip_b64:
        txs.append(tip_b64)
    return await get_jito_bundle_executor().send_bundle_b64(
        txs,
        tip_lamports=tip_lamports,
    )


async def _send_signed_rpc(signed_b64: str) -> dict[str, Any]:
    """Send a signed versioned tx directly via RPC with confirmation."""
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solana.rpc.types import TxOpts

    from src.core.rpc_config import call_with_rpc_fallback

    raw = base64.b64decode(signed_b64)

    async def _send(rpc: str) -> str:
        async with AsyncClient(rpc) as client:
            resp = await client.send_raw_transaction(
                raw,
                opts=TxOpts(
                    skip_preflight=False,
                    preflight_commitment=Confirmed,
                    max_retries=3,
                ),
            )
            if resp.value is None:
                raise RuntimeError(f"rpc_send_empty_sig: {resp}")
            sig = resp.value
            from solders.signature import Signature

            if isinstance(sig, str):
                sig_obj = Signature.from_string(sig)
            else:
                sig_obj = sig

            try:
                confirm_result = await client.confirm_transaction(
                    sig_obj,
                    commitment=Confirmed,
                )
                err = None
                if confirm_result.value:
                    statuses = confirm_result.value
                    if statuses and statuses[0] is not None:
                        err = statuses[0].err
                if err:
                    raise RuntimeError(f"rpc_confirm_failed: {err}")
            except RuntimeError:
                raise
            except Exception as exc:
                logger.warning("confirm_transaction failed: %s", exc)
                raise RuntimeError(f"rpc_confirm_failed: {exc}") from exc
            return str(sig_obj)

    try:
        sig = await call_with_rpc_fallback(
            "transaction",
            _send,
            label="jupiter_rpc_send",
        )
        logger.info("Jupiter swap sent via RPC | tx=%s", sig)
        return {
            "success": True,
            "txid": sig,
            "tx_sig": sig,
            "send_path": "rpc",
        }
    except Exception as exc:
        logger.warning("RPC swap send failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "step": "rpc_send_failed",
        }


async def send_signed_swap_transaction(
    signed_b64: str,
    *,
    tip_lamports: int,
    keypair: Any | None = None,
) -> dict[str, Any]:
    """
    Jito bundle first (with optional tip tx), then RPC fallback on landing failure.

    Hot-wallet v2 path: append tip tx so bundles land; fall back to RPC when Jito
    poll times out (common on small trades without a separate tip).
    """
    if signed_b64 == "simulated_tx":
        return {"success": True, "txid": "simulated_tx", "tx_sig": "simulated_tx"}

    use_jito = _env_bool("MEV_PROTECTION_ENABLED", True) and not _env_bool(
        "V2_RPC_ONLY_SEND", False
    )
    rpc_fallback = _env_bool("JITO_RPC_FALLBACK_ON_FAIL", True)

    tip_b64: str | None = None
    if (
        use_jito
        and keypair is not None
        and _env_bool("JITO_APPEND_TIP_TX", True)
    ):
        tip_b64 = await build_signed_jito_tip_b64(keypair, tip_lamports)

    if use_jito:
        result = await _send_signed_jito_bundle(
            signed_b64,
            tip_lamports=tip_lamports,
            tip_b64=tip_b64,
        )
        try:
            from src.core.jito_tip import record_jito_bundle_outcome

            record_jito_bundle_outcome(bool(result.get("success")))
        except Exception:
            pass
        if result.get("success"):
            result.setdefault("tip_lamports", tip_lamports)
            result["send_path"] = "jito"
            return result
        err = str(result.get("error") or "jito_send_failed")
        logger.warning(
            "Jito swap send failed | err=%s tip=%s had_tip_tx=%s",
            err,
            tip_lamports,
            bool(tip_b64),
        )
        if not rpc_fallback:
            result.setdefault("step", "jito_send_failed")
            return result

    rpc_result = await _send_signed_rpc(signed_b64)
    if rpc_result.get("success"):
        rpc_result.setdefault("tip_lamports", tip_lamports)
    else:
        rpc_result.setdefault("step", "jito_send_failed")
        rpc_result.setdefault("error", rpc_result.get("error") or "jito_send_failed")
    return rpc_result


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

def _jupiter_quote_url() -> str:
    return os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote").strip()


def _jupiter_swap_url() -> str:
    return os.getenv("JUPITER_SWAP_URL", "https://lite-api.jup.ag/swap/v1/swap").strip()


_BUILTIN_QUOTE_URLS: tuple[str, ...] = (
    "https://api.jup.ag/swap/v1/quote",
    "https://quote.jup.ag/v6/quote",
    "https://lite-api.jup.ag/swap/v1/quote",
)


def _quote_backoff_seconds(base_delay: float, attempt: int) -> float:
    """Exponential-ish backoff with 0–30% jitter."""
    return base_delay * (attempt + 1) * (1.0 + random.random() * 0.3)


def _quote_failure_kind(exc: Exception) -> str:
    """Classify quote errors — DNS/client reject skip retries on that host."""
    msg = str(exc).lower()
    if "400" in str(exc) or "404" in str(exc):
        return "client_reject"
    if any(
        needle in msg
        for needle in (
            "name or service not known",
            "getaddrinfo",
            "nodename nor servname",
            "temporary failure in name resolution",
        )
    ):
        return "dns"
    return "retryable"


def _append_unique(urls: list[str], *candidates: str) -> None:
    for raw in candidates:
        u = raw.strip().strip('"').strip("'")
        if u and u not in urls:
            urls.append(u)


def _parse_url_list(env_name: str, *defaults: str) -> list[str]:
    """Comma-separated URL list (legacy ``JUPITER_QUOTE_FALLBACK_URLS``)."""
    raw = (os.getenv(env_name) or "").strip()
    urls: list[str] = []
    if raw:
        _append_unique(urls, *raw.split(","))
    _append_unique(urls, *defaults)
    return urls


def _parse_urls_env(env_name: str) -> list[str]:
    """
    Parse ``JUPITER_QUOTE_URLS`` / ``JUPITER_SWAP_URLS``.

    Supports JSON array or comma-separated URLs.
    """
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                _append_unique(urls, *(str(x) for x in parsed))
        except json.JSONDecodeError:
            logger.warning("%s JSON parse failed, falling back to CSV", env_name)
    if not urls:
        _append_unique(urls, *raw.split(","))
    return urls


def _jupiter_quote_urls() -> list[str]:
    """Ordered quote endpoints: JUPITER_QUOTE_URLS → JUPITER_QUOTE_URL → fallbacks → builtins."""
    urls: list[str] = []
    _append_unique(urls, *_parse_urls_env("JUPITER_QUOTE_URLS"))
    primary = _jupiter_quote_url()
    if primary:
        _append_unique(urls, primary)
    _append_unique(urls, *_parse_url_list("JUPITER_QUOTE_FALLBACK_URLS"))
    _append_unique(urls, *_BUILTIN_QUOTE_URLS)
    return urls or list(_BUILTIN_QUOTE_URLS)


def _jupiter_swap_urls() -> list[str]:
    """Swap endpoints: explicit list, primary env, paired quote hosts, builtins."""
    urls: list[str] = []
    _append_unique(urls, *_parse_urls_env("JUPITER_SWAP_URLS"))
    primary = _jupiter_swap_url()
    if primary:
        _append_unique(urls, primary)
    _append_unique(urls, *_parse_url_list("JUPITER_SWAP_FALLBACK_URLS"))
    for quote_url in _jupiter_quote_urls():
        _append_unique(urls, quote_url_to_swap_url(quote_url))
    _append_unique(
        urls,
        "https://api.jup.ag/swap/v1/swap",
        "https://quote.jup.ag/v6/swap",
        "https://lite-api.jup.ag/swap/v1/swap",
    )
    return urls or ["https://lite-api.jup.ag/swap/v1/swap"]


def quote_url_to_swap_url(quote_url: str) -> str:
    """Map a quote endpoint to its swap sibling on the same host/path version."""
    q = quote_url.strip().rstrip("/")
    if q.endswith("/quote"):
        return f"{q[: -len('/quote')]}/swap"
    if "/quote" in q:
        return q.replace("/quote", "/swap", 1)
    return _jupiter_swap_url()


# Module-level aliases (refreshed in JupiterClient.__init__)
JUPITER_QUOTE_URL = _jupiter_quote_url()
JUPITER_SWAP_URL = _jupiter_swap_url()


@dataclass
class JupQuote:
    """Normalized quote (CEX-DEX probe: implied USDC per SOL)."""

    price: float
    raw: dict[str, Any] | None = None
    out_amount: int = 0
    route: list[Any] | None = None


def _load_jupiter_api_key(settings: Settings) -> str:
    from src.core.security import is_placeholder_secret, load_secrets_from_files, read_secret_file

    load_secrets_from_files()
    key = (os.getenv("JUPITER_API_KEY") or getattr(settings, "JUPITER_API_KEY", None) or "").strip()
    if key and not is_placeholder_secret(key):
        return key
    path = (os.getenv("JUPITER_API_KEY_FILE") or getattr(settings, "JUPITER_API_KEY_FILE", None) or "").strip()
    if not path:
        return ""
    try:
        raw = read_secret_file(Path(path))
        return "" if is_placeholder_secret(raw) else raw.strip()
    except OSError:
        return ""


def _headers(api_key: str) -> dict[str, str]:
    h = {"Accept": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def implied_usdc_per_sol(usdc_micro: int, out_lamports: int) -> float:
    """USDC (6 dp) → SOL (9 dp) quote → USDC price per 1 SOL."""
    return implied_usdc_per_base(usdc_micro, out_lamports, base_decimals=9)


def implied_usdc_per_base(
    usdc_micro: int,
    out_amount_raw: int,
    *,
    base_decimals: int,
) -> float:
    """USDC (6 dp) → base token quote → USDC price per 1 base unit."""
    if out_amount_raw <= 0 or usdc_micro <= 0:
        return 0.0
    usdc = usdc_micro / 1_000_000.0
    base = out_amount_raw / (10**int(base_decimals))
    if base <= 0:
        return 0.0
    return usdc / base


def implied_usdc_per_base_from_sell(
    base_amount_raw: int,
    usdc_out_micro: int,
    *,
    base_decimals: int,
) -> float:
    """Base → USDC sell quote → USDC price per 1 base unit."""
    if base_amount_raw <= 0 or usdc_out_micro <= 0:
        return 0.0
    usdc = usdc_out_micro / 1_000_000.0
    base = base_amount_raw / (10**int(base_decimals))
    if base <= 0:
        return 0.0
    return usdc / base


class JupiterClient:
    """Jupiter v6 REST client for CEX-DEX arbitrage."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = _load_jupiter_api_key(self.settings)
        self.client = httpx.AsyncClient(timeout=15.0)
        self.SOL = Pubkey.from_string(SOL_MINT)
        self.USDC = Pubkey.from_string(USDC_MINT)
        self.quote_urls = _jupiter_quote_urls()
        self.swap_urls = _jupiter_swap_urls()
        self.quote_url = self.quote_urls[0]
        self.swap_url = self.swap_urls[0]
        self.max_retries = max(1, int(os.getenv("JUPITER_QUOTE_MAX_RETRIES", "8")))
        self.retry_delay = float(os.getenv("JUPITER_QUOTE_RETRY_DELAY_SEC", "0.8"))
        self._keypair = self._load_keypair_optional()
        self._signer_type = (
            os.getenv("SIGNER_TYPE") or getattr(self.settings, "SIGNER_TYPE", None) or "hot"
        ).strip().lower()
        if self._keypair is None:
            logger.warning(
                "JupiterExecutor: no keypair — quote-only until PRIVATE_KEY_FILE / PRIVATE_KEY is available"
            )
        logger.info(
            "JupiterExecutor initialized (test_mode=%s quote_only=%s signer=%s)",
            self.settings.test_mode,
            self.quote_only,
            self._signer_type,
        )

    @property
    def keypair(self):
        return self._keypair

    @property
    def quote_only(self) -> bool:
        return self._keypair is None

    def _load_keypair_optional(self):
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
                    kp = secure_load_keypair(material)
                    logger.info("JupiterExecutor: loaded signing keypair from %s", env_name)
                    return kp
                except Exception as exc:
                    logger.warning("JupiterExecutor: %s invalid: %s", env_name, exc)
        return None

    async def has_signing(self) -> bool:
        return self._keypair is not None

    async def _sign_versioned(self, tx: VersionedTransaction) -> VersionedTransaction:
        if self._keypair is None:
            raise RuntimeError("No signing keypair loaded (SIGNER_TYPE=hot + PRIVATE_KEY_FILE)")
        sig = self._keypair.sign_message(to_bytes_versioned(tx.message))
        return VersionedTransaction.populate(tx.message, [sig])

    async def sign_swap_transaction_b64(self, swap_tx_b64: str) -> str | None:
        """Sign Jupiter swap tx (base64) via hot wallet keypair."""
        if self.settings.test_mode or self.settings.simulate:
            return swap_tx_b64
        if not await self.has_signing():
            logger.error("Cannot sign swap: no keypair loaded")
            return None
        try:
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed = await self._sign_versioned(tx)
            return base64.b64encode(bytes(signed)).decode()
        except Exception as exc:
            logger.error("Jupiter swap sign failed: %s", exc, exc_info=True)
            return None

    async def build_signed_tip_transaction(self, tip_lamports: int) -> VersionedTransaction | None:
        """Signed SOL transfer to a Jito tip account (hot keypair only)."""
        if self._keypair is None or int(tip_lamports) <= 0:
            return None
        tip_b64 = await build_signed_jito_tip_b64(self._keypair, int(tip_lamports))
        if not tip_b64:
            return None
        return VersionedTransaction.from_bytes(base64.b64decode(tip_b64))

    def _normalize_quote_payload(self, data: Any) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        if "outAmount" in data:
            return data
        inner = data.get("data")
        if isinstance(inner, dict) and "outAmount" in inner:
            return inner
        return None

    async def _fetch_quote_once(
        self,
        amount: int,
        *,
        quote_url: str | None = None,
        input_mint: str | None = None,
        output_mint: str | None = None,
        slippage_bps: int = 80,
        platform_fee_bps: int = 0,
    ) -> dict[str, Any] | None:
        """Single Jupiter quote HTTP attempt (no retries)."""
        in_mint = input_mint or str(self.SOL)
        out_mint = output_mint or str(self.USDC)
        eff_slippage = resolve_slippage_bps(in_mint, out_mint, override=slippage_bps)
        params = build_quote_query_params(
            amount,
            input_mint=in_mint,
            output_mint=out_mint,
            slippage_bps=eff_slippage,
            platform_fee_bps=platform_fee_bps,
        )
        url = (quote_url or self.quote_url).strip()
        resp = await self.client.get(
            url,
            params=params,
            headers=_headers(self.api_key),
        )
        resp.raise_for_status()
        return self._normalize_quote_payload(resp.json())

    async def get_quote_with_retry(
        self,
        amount: int,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Best-effort quote with throttle, retries, and None on exhaustion (no cycle kill)."""
        input_mint = kwargs.get("input_mint")
        output_mint = kwargs.get("output_mint")
        slippage_bps = int(
            kwargs.get(
                "slippage_bps",
                resolve_slippage_bps(
                    str(input_mint or self.SOL),
                    str(output_mint or self.USDC),
                ),
            )
        )
        platform_fee_bps = int(kwargs.get("platform_fee_bps", 0))

        last_exc: Exception | None = None
        primary_url = self.quote_urls[0] if self.quote_urls else ""

        for attempt in range(self.max_retries):
            for quote_url in self.quote_urls:
                is_fallback = bool(primary_url and quote_url != primary_url)
                try:
                    async with _quote_semaphore():
                        await _throttle_jupiter_quote()
                        quote = await self._fetch_quote_once(
                            amount,
                            quote_url=quote_url,
                            input_mint=input_mint,
                            output_mint=output_mint,
                            slippage_bps=slippage_bps,
                            platform_fee_bps=platform_fee_bps,
                        )
                    if quote and quote.get("outAmount"):
                        self.quote_url = quote_url
                        self.swap_url = quote_url_to_swap_url(quote_url)
                        if is_fallback:
                            logger.info("Jupiter quote ok via fallback %s", quote_url)
                        elif attempt > 0:
                            logger.info(
                                "Jupiter quote ok via %s (attempt %d)",
                                quote_url,
                                attempt + 1,
                            )
                        return quote
                    if attempt == self.max_retries - 1:
                        if is_fallback:
                            logger.warning(
                                "Jupiter fallback empty: %s",
                                quote_url,
                            )
                        else:
                            logger.warning(
                                "Jupiter empty quote from %s",
                                quote_url,
                            )
                except Exception as exc:
                    last_exc = exc
                    kind = _quote_failure_kind(exc)
                    if kind != "retryable":
                        logger.debug(
                            "Jupiter quote skip %s (%s): %s",
                            quote_url,
                            kind,
                            exc,
                        )
                        continue
                    if attempt == self.max_retries - 1:
                        if is_fallback:
                            logger.warning(
                                "Jupiter fallback failed: %s — %s",
                                quote_url,
                                exc,
                            )
                        else:
                            logger.warning(
                                "Jupiter quote failed (%s): %s",
                                quote_url,
                                exc,
                            )
                    else:
                        logger.debug(
                            "Jupiter quote retry (%s): %s, round %d/%d",
                            quote_url,
                            exc,
                            attempt + 1,
                            self.max_retries,
                        )

            if attempt < self.max_retries - 1:
                await asyncio.sleep(
                    _quote_backoff_seconds(self.retry_delay, attempt)
                )

        if last_exc is not None:
            if _quote_failure_kind(last_exc) == "client_reject":
                logger.debug("Jupiter quote unavailable for mint pair: %s", last_exc)
            else:
                logger.warning("Jupiter quote failed all URLs: %s", last_exc)
        else:
            logger.debug("Jupiter quote failed all URLs (empty responses)")
        return None

    async def fetch_quote_raw(
        self,
        amount: int,
        *,
        input_mint: str | None = None,
        output_mint: str | None = None,
        slippage_bps: int = 80,
        platform_fee_bps: int = 0,
    ) -> dict[str, Any] | None:
        """Raw Jupiter v6 quote response (retries + throttle)."""
        return await self.get_quote_with_retry(
            amount,
            input_mint=input_mint,
            output_mint=output_mint,
            slippage_bps=slippage_bps,
            platform_fee_bps=platform_fee_bps,
        )

    async def get_quote_dict(
        self,
        amount: int = 100_000_000,
        input_mint: str | None = None,
        output_mint: str | None = None,
        *,
        slippage_bps: int = 80,
    ) -> dict[str, Any] | None:
        """Dict quote (user schema) with ``price``, ``out_amount``, ``route``, ``quote``."""
        in_mint = input_mint or str(self.SOL)
        out_mint = output_mint or str(self.USDC)
        data = await self.fetch_quote_raw(
            amount,
            input_mint=in_mint,
            output_mint=out_mint,
            slippage_bps=slippage_bps,
        )
        if not data:
            return None

        out_amount = int(data["outAmount"])
        if in_mint == str(self.USDC) and out_mint == str(self.SOL):
            price = implied_usdc_per_sol(amount, out_amount)
        elif in_mint == str(self.SOL) and out_mint == str(self.USDC):
            sol = amount / 1_000_000_000.0
            usdc = out_amount / 1_000_000.0
            price = usdc / sol if sol > 0 else 0.0
        else:
            price = float(out_amount) / max(1, amount)

        return {
            "price": price,
            "out_amount": out_amount,
            "route": data.get("routePlan", []),
            "quote": data,
        }

    async def get_quote(
        self,
        amount: int = 100_000_000,
        *,
        slippage_bps: int = 40,
        input_mint: str | None = None,
        output_mint: str | None = None,
    ) -> JupQuote | None:
        """
        CEX-DEX probe default: USDC → SOL, returns implied USDC/SOL as ``JupQuote.price``.
        """
        in_mint = input_mint or str(self.USDC)
        out_mint = output_mint or str(self.SOL)
        parsed = await self.get_quote_dict(
            amount,
            input_mint=in_mint,
            output_mint=out_mint,
            slippage_bps=slippage_bps,
        )
        if not parsed:
            return None
        return JupQuote(
            price=float(parsed["price"]),
            raw=parsed.get("quote"),
            out_amount=int(parsed["out_amount"]),
            route=parsed.get("route"),
        )

    async def get_best_quote(
        self,
        input_amount: int,
        slippage_bps: int = 40,
        *,
        input_mint: str = SOL_MINT,
        output_mint: str = USDC_MINT,
    ) -> dict[str, Any] | None:
        """Legacy alias — returns raw quote dict."""
        return await self.fetch_quote_raw(
            input_amount,
            input_mint=input_mint,
            output_mint=output_mint,
            slippage_bps=slippage_bps,
        )

    async def get_implied_usdc_per_base(
        self,
        usdc_micro: int,
        base_mint: str,
        *,
        base_decimals: int = 9,
        slippage_bps: int | None = None,
        cex_reference: float | None = None,
    ) -> tuple[float | None, dict[str, Any] | None]:
        """USDC → base mint quote; returns USDC price per 1 base token."""
        from src.dex.jupiter_params import resolve_slippage_bps

        bps = (
            slippage_bps
            if slippage_bps is not None
            else resolve_slippage_bps(USDC_MINT, base_mint)
        )
        quote = await self.fetch_quote_raw(
            usdc_micro,
            input_mint=USDC_MINT,
            output_mint=base_mint,
            slippage_bps=bps,
        )
        if not quote or "outAmount" not in quote:
            return None, quote
        out_raw = int(quote["outAmount"])
        px = implied_usdc_per_base(usdc_micro, out_raw, base_decimals=base_decimals)
        if px <= 0:
            return None, quote
        if cex_reference is not None and not self._is_sane_price(px, float(cex_reference)):
            return None, quote
        return px, quote

    def _is_sane_price(self, jup_price: float, cex_bid: float) -> bool:
        """Reject garbage Jupiter implied prices vs CEX reference."""
        if jup_price <= 0 or cex_bid <= 0:
            return False
        try:
            tol = float(os.getenv("JUPITER_PRICE_SANITY_PCT", "0.08"))
        except (TypeError, ValueError):
            tol = 0.08
        rel = abs(jup_price - cex_bid) / cex_bid
        if rel > tol:
            logger.warning(
                "BAD_JUPITER_PRICE | jup_price=%s cex_bid=%s rel_diff=%.4f tol=%.4f",
                jup_price,
                cex_bid,
                rel,
                tol,
            )
            return False
        return True

    async def get_implied_usdc_per_base_sell(
        self,
        usdc_notional_micro: int,
        base_mint: str,
        cex_price_per_base: float,
        *,
        base_decimals: int = 9,
        slippage_bps: int | None = None,
    ) -> tuple[float | None, dict[str, Any] | None]:
        """
        CEX-buy notional → estimate base size → base→USDC Jupiter sell quote.

        Use for CEX-cheap detection and roundtrip sim (not USDC→base buy quotes).
        """
        import os

        from src.dex.jupiter_params import resolve_slippage_bps

        if usdc_notional_micro <= 0 or cex_price_per_base <= 0:
            return None, None
        fudge = float(os.getenv("CEX_DEX_CEX_BUY_FILL_FUDGE", "0.995"))
        base_raw = int(
            (usdc_notional_micro / 1_000_000.0)
            / cex_price_per_base
            * (10**int(base_decimals))
            * fudge
        )
        base_raw = max(base_raw, 1)
        bps = (
            slippage_bps
            if slippage_bps is not None
            else resolve_slippage_bps(base_mint, USDC_MINT)
        )
        quote = await self.fetch_quote_raw(
            base_raw,
            input_mint=base_mint,
            output_mint=USDC_MINT,
            slippage_bps=bps,
        )
        if not quote or "outAmount" not in quote:
            return None, quote
        usdc_out = int(quote["outAmount"])
        px = implied_usdc_per_base_from_sell(
            base_raw,
            usdc_out,
            base_decimals=base_decimals,
        )
        if px <= 0:
            return None, quote
        return px, quote

    async def get_implied_usdc_per_sol(
        self,
        usdc_micro: int,
        *,
        slippage_bps: int = 40,
    ) -> tuple[float | None, dict[str, Any] | None]:
        return await self.get_implied_usdc_per_base(
            usdc_micro,
            SOL_MINT,
            base_decimals=9,
            slippage_bps=slippage_bps,
        )

    async def get_price(self, amount_usdc_micro: int = 100_000_000) -> float | None:
        price, _ = await self.get_implied_usdc_per_sol(amount_usdc_micro)
        return price

    async def build_swap_transaction(
        self,
        quote_response: dict[str, Any],
        user_pubkey: str,
        *,
        slippage_bps: int = 100,
    ) -> dict[str, Any] | None:
        """Build versioned swap transaction (base64) for Jito / RPC send."""
        raw_quote = quote_response.get("quote") or quote_response
        payload = build_swap_request_body(
            raw_quote,
            user_pubkey,
            slippage_bps=slippage_bps,
        )
        swap_candidates = [self.swap_url]
        for u in self.swap_urls:
            if u not in swap_candidates:
                swap_candidates.append(u)

        last_exc: Exception | None = None
        for swap_url in swap_candidates:
            try:
                resp = await self.client.post(
                    swap_url,
                    json=payload,
                    headers=_headers(self.api_key),
                )
                resp.raise_for_status()
                data = resp.json()
                if data and "swapTransaction" in data:
                    self.swap_url = swap_url
                    return data
                logger.warning("Jupiter swap build empty from %s", swap_url)
            except Exception as exc:
                last_exc = exc
                logger.warning("Swap build failed (%s): %s", swap_url, exc)

        logger.error(
            "Swap build failed on all URLs%s",
            f" (last: {last_exc})" if last_exc else "",
        )
        return None

    async def execute_swap_with_jito(
        self,
        amount_micro: int,
        slippage_bps: int = 38,
        tip_lamports: int | None = None,
        *,
        input_mint: str = USDC_MINT,
        output_mint: str = SOL_MINT,
        net_bps: float | None = None,
        gross_bps: float | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """Quote → swap tx → Jito bundle."""
        tip = tip_lamports
        if tip is None:
            tip = resolve_execution_jito_tip_lamports(
                net_bps,
                size_usdc_micro=amount_micro,
                gross_bps=gross_bps,
                confidence=confidence,
            )
        try:
            if self.settings.simulate or self.settings.test_mode:
                logger.info(
                    "[SIMULATE] Jupiter swap %s micro | tip=%s",
                    amount_micro,
                    tip,
                )
                return {"success": True, "txid": "simulated_tx"}

            quote = await self.get_quote_with_retry(
                amount_micro,
                input_mint=input_mint,
                output_mint=output_mint,
                slippage_bps=slippage_bps,
            )
            if not quote:
                return {"success": False, "error": "quote_failed"}

            wallet = (
                self.settings.wallet_pubkey
                or self.settings.WALLET_PUBKEY
                or os.getenv("WALLET_PUBKEY", "")
            )
            if not wallet:
                return {"success": False, "error": "wallet_pubkey_missing"}

            swap_data = await self.build_swap_transaction(
                {"quote": quote},
                str(wallet),
                slippage_bps=slippage_bps,
            )
            if not swap_data or "swapTransaction" not in swap_data:
                return {"success": False, "error": "swap_tx_missing"}

            signed_b64 = await self.sign_swap_transaction_b64(swap_data["swapTransaction"])
            if not signed_b64:
                return {"success": False, "error": "swap_sign_failed"}

            tx_result = await send_signed_swap_transaction(
                signed_b64,
                tip_lamports=tip,
                keypair=self._keypair,
            )
            if tx_result.get("success"):
                logger.info(
                    "Jupiter swap confirmed | tx=%s tip=%s path=%s",
                    tx_result.get("txid"),
                    tip,
                    tx_result.get("send_path"),
                )
                return {"success": True, "tip_lamports": tip, **tx_result}
            return {"success": False, "tip_lamports": tip, **tx_result}
        except Exception as exc:
            logger.error("Jupiter execution error: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    async def execute_swap(
        self,
        amount_micro: int,
        slippage_bps: int = 40,
        *,
        net_bps: float | None = None,
    ) -> dict[str, Any]:
        return await self.execute_swap_with_jito(
            amount_micro,
            slippage_bps=slippage_bps,
            net_bps=net_bps,
        )

    async def swap_usdc_to_sol(self, size_usdc: int) -> dict[str, Any]:
        """Multi-venue helper: spend USDC notional (micro) for SOL."""
        micro = int(size_usdc)
        if micro < 1_000_000 and size_usdc > 0:
            micro = int(size_usdc * 1_000_000)
        return await self.execute_swap_with_jito(
            micro,
            input_mint=USDC_MINT,
            output_mint=SOL_MINT,
        )

    async def execute_buy_sol(
        self,
        amount_usdc: int,
        *,
        slippage_bps: int | None = None,
        net_bps: float | None = None,
    ) -> dict[str, Any]:
        """Buy SOL with USDC (micro) via Jupiter + Jito; returns ``sol_received`` in SOL units."""
        micro = int(amount_usdc)
        if micro < 1_000_000 and amount_usdc > 0:
            micro = int(amount_usdc * 1_000_000)
        bps = (
            int(slippage_bps)
            if slippage_bps is not None
            else resolve_slippage_bps(USDC_MINT, SOL_MINT)
        )
        out_lamports = 0
        if not (self.settings.simulate or self.settings.test_mode):
            quote = await self.get_quote_with_retry(
                micro,
                input_mint=USDC_MINT,
                output_mint=SOL_MINT,
                slippage_bps=bps,
            )
            if quote and "outAmount" in quote:
                out_lamports = int(quote["outAmount"])

        result = await self.execute_swap_with_jito(
            micro,
            slippage_bps=bps,
            input_mint=USDC_MINT,
            output_mint=SOL_MINT,
            net_bps=net_bps,
        )
        sol_received = out_lamports / 1_000_000_000.0 if out_lamports > 0 else 0.0
        if result.get("success") and sol_received <= 0 and micro > 0:
            sol_received = (micro / 1_000_000.0) / max(
                1.0,
                float(os.getenv("DEX_CEX_REVERSE_SOL_PRICE_FALLBACK", "150")),
            )
        tx_sig = str(
            result.get("tx_sig") or result.get("txid") or result.get("bundle_id") or ""
        )
        return {
            **result,
            "tx_sig": tx_sig,
            "amount_lamports": out_lamports,
            "sol_received": sol_received,
        }

    async def sell_sol(
        self,
        amount_lamports: int,
        *,
        slippage_bps: int | None = None,
        tip_lamports: int | None = None,
        net_bps: float | None = None,
        gross_bps: float | None = None,
        size_usdc_micro: int | None = None,
        rpc_only: bool = False,
    ) -> dict[str, Any]:
        """Sell SOL for USDC via Jupiter quote → signed swap → Jito bundle."""
        lamports = max(1, int(amount_lamports))
        bps = (
            int(slippage_bps)
            if slippage_bps is not None
            else resolve_slippage_bps(SOL_MINT, USDC_MINT)
        )
        tip = tip_lamports
        if tip is None:
            micro = size_usdc_micro
            if micro is None and net_bps is not None:
                sol_usd = float(os.getenv("JITO_TIP_SOL_USD", "180"))
                micro = int(lamports / 1_000_000_000.0 * sol_usd * 1_000_000)
            tip = resolve_execution_jito_tip_lamports(
                net_bps,
                size_usdc_micro=micro,
                gross_bps=gross_bps,
            )

        if self.settings.simulate or self.settings.test_mode:
            logger.info(
                "[SIMULATE] Jupiter sell_sol | lamports=%s slippage=%s",
                lamports,
                bps,
            )
            return {
                "success": True,
                "txid": "simulated_sell_sol",
                "tx_sig": "simulated_sell_sol",
                "out_usdc_micro": 0,
                "amount_lamports": lamports,
            }

        quote = await self.fetch_quote_raw(
            lamports,
            input_mint=SOL_MINT,
            output_mint=USDC_MINT,
            slippage_bps=bps,
        )
        if not quote or "outAmount" not in quote:
            return {"success": False, "error": "sell_quote_failed"}

        wallet = (
            self.settings.wallet_pubkey
            or self.settings.WALLET_PUBKEY
            or os.getenv("WALLET_PUBKEY", "")
        )
        if not wallet:
            return {"success": False, "error": "wallet_pubkey_missing"}

        swap_data = await self.build_swap_transaction(
            {"quote": quote},
            str(wallet),
            slippage_bps=bps,
        )
        if not swap_data or "swapTransaction" not in swap_data:
            return {"success": False, "error": "swap_tx_missing"}

        signed_b64 = await self.sign_swap_transaction_b64(swap_data["swapTransaction"])
        if not signed_b64:
            return {"success": False, "error": "swap_sign_failed"}

        prev_rpc_only = os.getenv("V2_RPC_ONLY_SEND")
        if rpc_only:
            os.environ["V2_RPC_ONLY_SEND"] = "true"
        try:
            tx_result = await send_signed_swap_transaction(
                signed_b64,
                tip_lamports=tip,
                keypair=self._keypair,
            )
        finally:
            if rpc_only:
                if prev_rpc_only is None:
                    os.environ.pop("V2_RPC_ONLY_SEND", None)
                else:
                    os.environ["V2_RPC_ONLY_SEND"] = prev_rpc_only
        out_usdc_micro = int(quote.get("outAmount", 0))
        tx_sig = str(
            tx_result.get("txid")
            or tx_result.get("bundle_id")
            or tx_result.get("tx_sig")
            or ""
        )
        if tx_result.get("success"):
            logger.info(
                "Jupiter sell_sol confirmed | lamports=%s out_usdc_micro=%s tx=%s",
                lamports,
                out_usdc_micro,
                tx_sig,
            )
            return {
                "success": True,
                "txid": tx_sig,
                "tx_sig": tx_sig,
                "out_usdc_micro": out_usdc_micro,
                "amount_lamports": lamports,
                **tx_result,
            }
        return {
            "success": False,
            "error": tx_result.get("error", "jito_send_failed"),
            "out_usdc_micro": out_usdc_micro,
            "amount_lamports": lamports,
            **tx_result,
        }

    async def get_quote_for_mints(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        *,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        """Raw Jupiter quote dict — ``(input_mint, output_mint, amount)`` strategy API."""
        bps = (
            int(slippage_bps)
            if slippage_bps is not None
            else resolve_slippage_bps(input_mint, output_mint)
        )
        quote = await self.fetch_quote_raw(
            int(amount),
            input_mint=input_mint,
            output_mint=output_mint,
            slippage_bps=bps,
        )
        if not quote:
            raise RuntimeError("Jupiter quote failed")
        logger.debug("Jupiter quote received | outAmount=%s", quote.get("outAmount"))
        return quote

    async def swap(self, quote: dict[str, Any], signer: Any) -> bool:
        """Build, sign, and send a Jupiter swap from a prior quote dict."""
        try:
            if self.settings.simulate or self.settings.test_mode:
                logger.info("[SIMULATE] Jupiter swap")
                return True

            wallet = str(signer.pubkey())
            raw_quote = quote.get("quote") if isinstance(quote.get("quote"), dict) else quote
            swap_data = await self.build_swap_transaction(
                {"quote": raw_quote},
                wallet,
            )
            if not swap_data or "swapTransaction" not in swap_data:
                logger.error("Jupiter swap failed: missing swapTransaction")
                return False

            tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)
            sig = signer.sign_message(to_bytes_versioned(tx.message))
            signed = VersionedTransaction.populate(tx.message, [sig])
            signed_b64 = base64.b64encode(bytes(signed)).decode()

            tip = resolve_execution_jito_tip_lamports(None)
            tx_result = await send_signed_swap_transaction(
                signed_b64,
                tip_lamports=tip,
                keypair=signer,
            )
            if tx_result.get("success"):
                logger.info(
                    "Jupiter swap sent | sig=%s",
                    tx_result.get("txid") or tx_result.get("tx_sig"),
                )
                return True
            logger.error("Jupiter swap failed: %s", tx_result.get("error"))
            return False
        except Exception as exc:
            logger.error("Jupiter swap failed: %s", exc)
            return False

    async def close(self) -> None:
        await self.client.aclose()


# Legacy alias used across strategies
JupiterExecutor = JupiterClient

_jupiter_client: JupiterClient | None = None


def get_jupiter_executor(settings: Settings | None = None) -> JupiterClient:
    global _jupiter_client
    if _jupiter_client is None:
        _jupiter_client = JupiterClient(settings)
    return _jupiter_client
