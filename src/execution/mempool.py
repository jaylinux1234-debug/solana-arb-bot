# mempool_watcher.py — chain-liveness / preconfirmation hooks for backrun workflows.
#
# Solana does not expose an Ethereum-style public mempool. This module keeps a
# lightweight slot heartbeat (and optional WS subscription) so you can extend
# it with log subscriptions, ShredStream, or victim-tx detection later.
from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp
from solana.rpc.async_api import AsyncClient

logger = logging.getLogger(__name__)

MEMPOOL_POLL_SEC = float(os.getenv("MEMPOOL_POLL_SECONDS", "2.0"))
MEMPOOL_STATUS_INTERVAL_SEC = float(os.getenv("MEMPOOL_STATUS_INTERVAL_SEC", "60"))


def _rpc_http_to_ws(url: str) -> str | None:
    if not url:
        return None
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return None


async def _slot_subscribe_loop(ws_url: str) -> None:
    """Best-effort slot stream for denser updates than polling."""
    req_sub = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe", "params": []})
    last_slot: int | None = None
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                await ws.send_str(req_sub)
                logger.info("Mempool watcher: subscribed to slots via %s", ws_url.split("@")[0])
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning("Mempool watcher WS error: %s", ws.exception())
                        break
                    try:
                        data = json.loads(msg.data)
                        params = data.get("params") or {}
                        result = params.get("result") or {}
                        slot = result.get("slot")
                        if isinstance(slot, int):
                            last_slot = slot
                    except json.JSONDecodeError:
                        continue
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Mempool watcher WS ended (%s); falling back to polling if loop continues.", exc
        )
    finally:
        if last_slot is not None:
            logger.debug("Mempool watcher WS last_slot=%s", last_slot)


async def mempool_watch_loop(
    client: AsyncClient,
    *,
    poll_sec: float = MEMPOOL_POLL_SEC,
    status_interval_sec: float = MEMPOOL_STATUS_INTERVAL_SEC,
) -> None:
    """
    Run until cancelled. Logs chain progression for operational visibility.
    When USE_SLOT_WS=true and SOLANA_WS_URL or RPC can be converted to wss://, also opens slotSubscribe.
    """
    ws_url = (os.getenv("SOLANA_WS_URL") or "").strip() or (
        _rpc_http_to_ws(os.getenv("SOLANA_RPC_URL") or "") or ""
    )
    use_ws = os.getenv("USE_SLOT_WS", "false").lower() == "true" and bool(ws_url)

    ws_task: asyncio.Task[None] | None = None
    if use_ws:
        ws_task = asyncio.create_task(_slot_subscribe_loop(ws_url))

    last_status = 0.0
    last_slot: int = -1
    try:
        logger.info(
            "Mempool watcher started (poll=%ss status_every=%ss ws=%s)",
            poll_sec,
            status_interval_sec,
            use_ws,
        )
        while True:
            try:
                resp = await client.get_slot()
                slot = int(resp.value)
                if slot != last_slot:
                    logger.debug("chain slot=%s", slot)
                    last_slot = slot
                now = asyncio.get_running_loop().time()
                if now - last_status >= status_interval_sec:
                    logger.info("Mempool watcher heartbeat slot=%s", slot)
                    last_status = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Mempool watcher tick failed: %s", exc)
            await asyncio.sleep(poll_sec)
    finally:
        if ws_task is not None:
            ws_task.cancel()
            await asyncio.gather(ws_task, return_exceptions=True)


def start_mempool_watcher(client: AsyncClient) -> asyncio.Task[None]:
    """Spawn background task; cancel it when shutting down the bot."""
    return asyncio.create_task(mempool_watch_loop(client))
