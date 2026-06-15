# helius_webhook.py — Helius webhook receiver + optional programmatic listener / backrun hooks.
#
# Dashboard setup (https://dashboard.helius.dev/webhooks):
#   Enhanced Transaction Webhooks, type SWAP, monitor Jupiter program
#   JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 (confirm in Jupiter docs).
#
# Env: .env.example — HELIUS_WEBHOOK_*, HELIUS_API_KEY, ENABLE_HELIUS_WEBHOOK_BACKRUN, …
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    from src.dex.jupiter import JupiterExecutor
    from src.execution.arbitrage import ArbitrageDetector
    from src.execution.jito import JitoBundleExecutor

logger = logging.getLogger(__name__)

_helius_runner: web.AppRunner | None = None
_helius_site: web.TCPSite | None = None
_uvicorn_server: Any | None = None

_webhook_listener: Any | None = None

swap_event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
_BASE_MINTS = frozenset({USDC_MINT, SOL_MINT})


def resolve_helius_api_key() -> str:
    """Prefer HELIUS_API_KEY; else parse api-key= from SOLANA_RPC_URL (Helius-style RPC URL)."""
    k = (os.getenv("HELIUS_API_KEY") or "").strip()
    if k:
        return k
    rpc = os.getenv("SOLANA_RPC_URL") or ""
    if "api-key=" in rpc:
        return rpc.split("api-key=", 1)[-1].split("&")[0].split("?")[0].strip()
    return ""


def register_helius_webhook_listener(listener: Any | None) -> None:
    """
    Register HeliusWebhookListener (or None to clear).
    Call from main.py when ENABLE_HELIUS_WEBHOOK_BACKRUN is enabled.
    """
    global _webhook_listener
    _webhook_listener = listener


def _jupiter_sources_from_env() -> frozenset[str]:
    raw = (os.getenv("HELIUS_JUPITER_SOURCES") or "JUPITER").upper()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def verify_helius_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header:
        return False
    sig = signature_header.strip()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(digest, sig)
    except Exception:
        return False


def verify_helius_http_request(raw_body: bytes, headers: Any) -> tuple[bool, str]:
    """Validate signature / auth for raw webhook POST body. Returns (ok, error_text)."""
    secret = (os.getenv("HELIUS_WEBHOOK_SECRET") or "").strip()
    require_sig = os.getenv("HELIUS_WEBHOOK_REQUIRE_SIGNATURE", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    sig_hdr = None
    if headers is not None and hasattr(headers, "get"):
        sig_hdr = headers.get("X-Helius-Signature") or headers.get("x-helius-signature")

    if secret and require_sig:
        if not verify_helius_signature(raw_body, sig_hdr, secret):
            return False, "invalid signature"

    auth_expected = (os.getenv("HELIUS_WEBHOOK_AUTH_TOKEN") or "").strip()
    if auth_expected and headers is not None and hasattr(headers, "get"):
        auth = headers.get("Authorization") or ""
        if auth != f"Bearer {auth_expected}" and auth != auth_expected:
            return False, "unauthorized"

    return True, ""


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        return [payload]
    return []


def _is_monitored_jupiter_swap(rec: dict[str, Any], sources: frozenset[str]) -> bool:
    if rec.get("type") != "SWAP":
        return False
    src = str(rec.get("source") or "").upper()
    return src in sources


def ingest_helius_payload(payload: Any) -> int:
    """Filter Jupiter SWAP rows, enqueue summaries, schedule optional backrun. Returns count."""
    sources = _jupiter_sources_from_env()
    jupiter_swaps: list[dict[str, Any]] = []

    for rec in _extract_records(payload):
        if not _is_monitored_jupiter_swap(rec, sources):
            continue
        jupiter_swaps.append(rec)
        summary = {
            "signature": rec.get("signature"),
            "slot": rec.get("slot"),
            "timestamp": rec.get("timestamp"),
            "source": rec.get("source"),
            "type": rec.get("type"),
            "description": (rec.get("description") or "")[:500],
            "feePayer": rec.get("feePayer"),
            "raw": rec,
        }
        logger.info(
            "Helius Jupiter SWAP | sig=%s slot=%s",
            summary.get("signature"),
            summary.get("slot"),
        )
        try:
            swap_event_queue.put_nowait(summary)
        except asyncio.QueueFull:
            logger.warning("swap_event_queue full — dropping Jupiter SWAP event")

    if not jupiter_swaps:
        logger.debug("Helius webhook payload contained no Jupiter SWAP events")

    schedule_listener_jobs(jupiter_swaps)
    return len(jupiter_swaps)


def schedule_listener_jobs(jupiter_swaps: list[dict[str, Any]]) -> None:
    listener = _webhook_listener
    if listener is None or not jupiter_swaps:
        return

    async def _run() -> None:
        try:
            await listener.process_jupiter_swaps(jupiter_swaps)
        except Exception:
            logger.exception("HeliusWebhookListener.process_jupiter_swaps failed")

    asyncio.create_task(_run())


def _estimate_swap_notional_micro(rec: dict[str, Any]) -> int:
    """Best-effort size heuristic from enhanced webhook fields (micro-ish units)."""
    best = 0
    for acc in rec.get("accountData") or []:
        for ch in acc.get("tokenBalanceChanges") or []:
            raw = ch.get("rawTokenAmount") or {}
            amt = raw.get("tokenAmount")
            if amt is None:
                continue
            try:
                best = max(best, abs(int(str(amt))))
            except (ValueError, TypeError):
                continue
    for tt in rec.get("tokenTransfers") or []:
        ta = tt.get("tokenAmount")
        if ta is None:
            continue
        try:
            best = max(best, abs(int(float(ta))))
        except (ValueError, TypeError):
            continue
    # Fallback: shallow tokenBalanceChanges on root (alternate shapes)
    for ch in rec.get("tokenBalanceChanges") or []:
        raw = ch.get("rawTokenAmount") if isinstance(ch, dict) else None
        if isinstance(raw, dict):
            amt = raw.get("tokenAmount")
        else:
            amt = None
        if amt is None:
            continue
        try:
            best = max(best, abs(int(str(amt))))
        except (ValueError, TypeError):
            continue
    return best


def _extract_midcap_mint(rec: dict[str, Any]) -> str | None:
    """Pick a non-base mint involved in balance / transfer noise."""
    candidates: list[str] = []
    for acc in rec.get("accountData") or []:
        for ch in acc.get("tokenBalanceChanges") or []:
            if not isinstance(ch, dict):
                continue
            m = ch.get("mint")
            if m and isinstance(m, str) and m not in _BASE_MINTS:
                candidates.append(m)
    for tt in rec.get("tokenTransfers") or []:
        if not isinstance(tt, dict):
            continue
        m = tt.get("mint")
        if m and isinstance(m, str) and m not in _BASE_MINTS:
            candidates.append(m)
    for ch in rec.get("tokenBalanceChanges") or []:
        if isinstance(ch, dict):
            m = ch.get("mint")
            if m and m not in _BASE_MINTS:
                candidates.append(m)
    if not candidates:
        return None
    # Prefer sole midcap; else stable sort tie-break
    uniq = sorted(set(candidates))
    return uniq[0]


class HeliusWebhookListener:
    """
    Optional programmatic webhook registration + backrun wiring.

    Typical construction::

        HeliusWebhookListener(jito_helper, jupiter_executor, arbitrage_detector)

    Use register_helius_webhook_listener(...) after creating this instance.
    """

    def __init__(
        self,
        jito_backrunner: JitoBundleExecutor,
        executor: JupiterExecutor,
        arbitrage_detector: ArbitrageDetector,
    ):
        self.jito = jito_backrunner
        self.executor = executor
        self.detector = arbitrage_detector
        self.webhook_id: str | None = None

    async def handle_webhook(self, payload: Any) -> None:
        """Process decoded Helius JSON (same work as POST to aiohttp/FastAPI)."""
        ingest_helius_payload(payload)

    def webhook_target_url(self) -> str:
        """Public URL Helius must POST to (tunnel + path)."""
        public_base = (os.getenv("HELIUS_WEBHOOK_PUBLIC_URL") or "").rstrip("/")
        fastapi_mode = os.getenv("ENABLE_HELIUS_FASTAPI", "").lower() in ("1", "true", "yes")
        if fastapi_mode:
            path = (
                os.getenv("FASTAPI_HELIUS_WEBHOOK_PATH")
                or os.getenv("HELIUS_WEBHOOK_PATH")
                or "/helius/webhook"
            ).strip()
            if not path.startswith("/"):
                path = "/" + path
            if public_base:
                if public_base.endswith(path) or public_base.endswith(path.rstrip("/")):
                    return public_base
                return f"{public_base}{path}"
            host = os.getenv("FASTAPI_WEBHOOK_BIND_DISPLAY_HOST") or "127.0.0.1"
            port = int(os.getenv("FASTAPI_WEBHOOK_PORT", "8000"))
            return f"http://{host}:{port}{path}"

        path = (os.getenv("HELIUS_WEBHOOK_PATH") or "/helius/webhook").strip()
        if not path.startswith("/"):
            path = "/" + path
        if public_base:
            if public_base.endswith(path) or public_base.endswith(path.rstrip("/")):
                return public_base
            return f"{public_base}{path}"
        host = os.getenv("HELIUS_WEBHOOK_BIND_DISPLAY_HOST") or "127.0.0.1"
        port = int(os.getenv("HELIUS_WEBHOOK_PORT", "8799"))
        return f"http://{host}:{port}{path}"

    async def create_webhook(self) -> None:
        """Create webhook via Helius HTTP API (requires HELIUS_API_KEY)."""
        api_key = resolve_helius_api_key()
        if not api_key:
            logger.error(
                "create_webhook: missing HELIUS_API_KEY (and no api-key in SOLANA_RPC_URL)"
            )
            return

        url = f"https://api-mainnet.helius-rpc.com/v0/webhooks?api-key={api_key}"
        raw_addrs = (os.getenv("HELIUS_WEBHOOK_MONITOR_ACCOUNTS") or "").strip()
        account_addresses = [a.strip() for a in raw_addrs.split(",") if a.strip()]

        webhook_url = self.webhook_target_url()
        payload = {
            "webhookURL": webhook_url,
            "transactionTypes": ["SWAP"],
            "accountAddresses": account_addresses,
            "webhookType": "enhanced",
            "txnStatus": (os.getenv("HELIUS_WEBHOOK_TXN_STATUS") or "success"),
        }

        try:
            async with aiohttp.ClientSession() as session, session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error("create_webhook non-JSON HTTP %s: %s", resp.status, text[:500])
                    return
        except Exception as exc:
            logger.exception("create_webhook request failed: %s", exc)
            return

        wid = data.get("webhookID") or data.get("webhookId")
        if wid:
            self.webhook_id = str(wid)
            logger.warning(
                "Helius webhook created id=%s target=%s",
                self.webhook_id,
                webhook_url,
            )
            return

        err_obj = data.get("error")
        if isinstance(err_obj, dict):
            err_msg = str(err_obj.get("message") or err_obj)
        else:
            err_msg = str(data.get("message") or err_obj or data)

        existing = await self._find_webhook_by_url(api_key, webhook_url)
        if existing:
            self.webhook_id = existing
            logger.info(
                "Helius webhook already registered id=%s target=%s",
                existing,
                webhook_url,
            )
            return

        if "already" in err_msg.lower() or data.get("statusCode") == 409:
            logger.warning(
                "Helius webhook duplicate hint but no matching URL in account | target=%s",
                webhook_url,
            )
            return

        if "max usage" in err_msg.lower():
            logger.warning(
                "Helius webhook quota reached — reuse dashboard webhook or delete stale entries | "
                "target=%s err=%s",
                webhook_url,
                err_msg,
            )
            return

        logger.error("Helius webhook creation failed: %s", data)

    async def _find_webhook_by_url(self, api_key: str, webhook_url: str) -> str | None:
        list_url = f"https://api-mainnet.helius-rpc.com/v0/webhooks?api-key={api_key}"
        try:
            async with aiohttp.ClientSession() as session, session.get(
                list_url, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                if resp.status != 200:
                    return None
                rows = await resp.json()
        except Exception:
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("webhookURL") or "").rstrip("/") == webhook_url.rstrip("/"):
                wid = row.get("webhookID") or row.get("webhookId")
                if wid:
                    return str(wid)
        return None

    async def backrun_jupiter_opportunity(
        self,
        *,
        midcap_mint: str,
        detected_amount: int,
    ) -> str | None:
        """Quote USDC→mint→SOL→USDC, gate profitability, submit Jito bundle."""
        from src.strategies.backrun_executor import get_backrun_executor

        victim_ctx = {
            "amount_micro": detected_amount,
            "midcap_mint": midcap_mint,
        }
        ok = await get_backrun_executor(
            settings=self.executor.settings if hasattr(self.executor, "settings") else None
        ).execute(victim_ctx)
        return "backrun_ok" if ok else None

    async def process_jupiter_swaps(self, records: list[dict[str, Any]]) -> None:
        """Filter large Jupiter SWAP enhanced records and trigger backrun."""
        min_amt = int(os.getenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000"))
        frac = float(os.getenv("HELIUS_BACKRUN_AMOUNT_FRACTION", "0.8"))

        for rec in records:
            notion = _estimate_swap_notional_micro(rec)
            if notion < min_amt:
                continue
            midcap = _extract_midcap_mint(rec)
            if not midcap:
                logger.debug("Helius backrun: could not infer midcap sig=%s", rec.get("signature"))
                continue

            amt = max(1, int(notion * frac))
            logger.info(
                "Helius Jupiter SWAP backrun candidate | mint=%s… notion≈%s frac_amt=%s sig=%s",
                midcap[:8],
                notion,
                amt,
                rec.get("signature"),
            )
            await self.backrun_jupiter_opportunity(midcap_mint=midcap, detected_amount=amt)


async def _handle_health(_request: web.Request) -> web.Response:
    return web.Response(text="ok", status=200)


async def _handle_webhook(request: web.Request) -> web.Response:
    raw = await request.read()

    ok, err = verify_helius_http_request(raw, request.headers)
    if not ok:
        logger.warning("Helius webhook rejected: %s", err)
        return web.Response(status=401, text=err)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Helius webhook: invalid JSON body")
        return web.Response(status=400, text="invalid json")

    from src.events.webhook.helius_handler import handle_helius_webhook_payload

    result = await handle_helius_webhook_payload(payload)
    return web.Response(status=200, text=json.dumps(result), content_type="application/json")


def create_helius_app() -> web.Application:
    path = (os.getenv("HELIUS_WEBHOOK_PATH") or "/helius/webhook").rstrip("/") or "/helius/webhook"
    health_path = (os.getenv("HELIUS_WEBHOOK_HEALTH_PATH") or "/helius/health").rstrip(
        "/"
    ) or "/helius/health"

    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post(path, _handle_webhook)
    app.router.add_get(health_path, _handle_health)

    logger.info(
        "Helius webhook routes POST %s GET %s (sources=%s)",
        path,
        health_path,
        ",".join(sorted(_jupiter_sources_from_env())),
    )
    return app


async def start_helius_webhook_server() -> None:
    global _helius_runner, _helius_site

    host = os.getenv("HELIUS_WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("HELIUS_WEBHOOK_PORT", "8799"))

    if _helius_runner is not None:
        logger.warning("Helius webhook server already started")
        return

    app = create_helius_app()
    _helius_runner = web.AppRunner(app)
    await _helius_runner.setup()
    _helius_site = web.TCPSite(_helius_runner, host=host, port=port)
    await _helius_site.start()
    logger.warning(
        "Helius webhook listening on http://%s:%s (expose HTTPS via tunnel for dashboard)",
        host,
        port,
    )


async def stop_helius_webhook_server() -> None:
    global _helius_runner, _helius_site
    if _helius_site is not None:
        await _helius_site.stop()
        _helius_site = None
    if _helius_runner is not None:
        await _helius_runner.cleanup()
        _helius_runner = None


async def helius_webhook_server_loop() -> None:
    await start_helius_webhook_server()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await stop_helius_webhook_server()
        raise


def start_helius_webhook_background() -> asyncio.Task[None]:
    return asyncio.create_task(helius_webhook_server_loop())


# ----- Optional FastAPI + uvicorn (ENABLE_HELIUS_FASTAPI=true) -----


def build_fastapi_helius_app() -> Any:
    """FastAPI app for POST webhook (requires ``pip install fastapi uvicorn``)."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI not installed. Run: pip install fastapi uvicorn[standard]"
        ) from exc

    path = (
        os.getenv("FASTAPI_HELIUS_WEBHOOK_PATH")
        or os.getenv("HELIUS_WEBHOOK_PATH")
        or "/webhook"
    ).rstrip("/") or "/webhook"
    health = (
        os.getenv("FASTAPI_HELIUS_HEALTH_PATH")
        or os.getenv("HELIUS_WEBHOOK_HEALTH_PATH")
        or "/health"
    ).rstrip("/") or "/health"

    from src.events.webhook.helius_handler import router as helius_router

    app = FastAPI(title="Helius webhook")
    app.include_router(helius_router)

    # Legacy path alias when dashboard points elsewhere (e.g. /webhook)
    if path != "/helius/webhook":

        @app.post(path)
        async def webhook_alias(request: Request) -> Any:
            from src.events.webhook.helius_handler import helius_webhook

            return await helius_webhook(request)

    @app.get(health)
    async def fastapi_health() -> dict[str, str]:
        return {"status": "ok"}

    logger.info(
        "FastAPI Helius routes POST /helius/webhook GET /helius/health "
        "(alias POST %s GET %s)",
        path,
        health,
    )
    return app


async def run_fastapi_helius_uvicorn() -> None:
    global _uvicorn_server
    try:
        import uvicorn
    except ImportError as exc:
        logger.error("uvicorn not installed: %s", exc)
        return

    app = build_fastapi_helius_app()
    host = os.getenv("FASTAPI_WEBHOOK_HOST") or os.getenv("HELIUS_WEBHOOK_HOST", "0.0.0.0")
    port = int(
        os.getenv("FASTAPI_WEBHOOK_PORT")
        or os.getenv("HELIUS_WEBHOOK_PORT", "8000")
    )
    log_level = (os.getenv("UVICORN_LOG_LEVEL") or "info").lower()
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    _uvicorn_server = server
    await server.serve()


async def stop_fastapi_helius_uvicorn() -> None:
    global _uvicorn_server
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
        _uvicorn_server = None


def start_fastapi_helius_background() -> asyncio.Task[None]:
    return asyncio.create_task(run_fastapi_helius_uvicorn())
