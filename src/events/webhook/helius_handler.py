# src/events/webhook/helius_handler.py
"""Helius enhanced webhook — real-time Jupiter SWAP ingest + priority backrun."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.events.bus import get_event_bus
from src.events.types import BotEvent, EventKind
from src.execution.helius import (
    _estimate_swap_notional_micro,
    _extract_midcap_mint,
    _extract_records,
    _is_monitored_jupiter_swap,
    _jupiter_sources_from_env,
    _webhook_listener,
    ingest_helius_payload,
    verify_helius_http_request,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class BackrunSignal:
    """Parsed backrun candidate from a Helius enhanced SWAP event."""

    midcap_mint: str
    amount_micro: int
    signature: str | None = None
    slot: int | None = None
    raw: dict[str, Any] | None = None


def is_backrun_opportunity(data: Any) -> bool:
    """True when payload contains a large monitored Jupiter SWAP (backrun lane)."""
    if os.getenv("ENABLE_HELIUS_WEBHOOK_BACKRUN", "false").lower() not in (
        "1",
        "true",
        "yes",
    ):
        return False

    sources = _jupiter_sources_from_env()
    min_amt = int(os.getenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000"))

    for rec in _extract_records(data):
        if not _is_monitored_jupiter_swap(rec, sources):
            continue
        notion = _estimate_swap_notional_micro(rec)
        if notion >= min_amt and _extract_midcap_mint(rec):
            return True
    return False


def parse_backrun_signal(data: Any) -> BackrunSignal | None:
    """Pick the largest qualifying Jupiter SWAP in the payload."""
    sources = _jupiter_sources_from_env()
    min_amt = int(os.getenv("HELIUS_BACKRUN_MIN_AMOUNT_MICRO", "50000000"))
    frac = float(os.getenv("HELIUS_BACKRUN_AMOUNT_FRACTION", "0.8"))

    best_rec: dict[str, Any] | None = None
    best_notion = 0

    for rec in _extract_records(data):
        if not _is_monitored_jupiter_swap(rec, sources):
            continue
        notion = _estimate_swap_notional_micro(rec)
        if notion < min_amt:
            continue
        midcap = _extract_midcap_mint(rec)
        if not midcap:
            continue
        if notion > best_notion:
            best_notion = notion
            best_rec = rec

    if not best_rec or best_notion <= 0:
        return None

    midcap = _extract_midcap_mint(best_rec)
    if not midcap:
        return None

    return BackrunSignal(
        midcap_mint=midcap,
        amount_micro=max(1, int(best_notion * frac)),
        signature=best_rec.get("signature"),
        slot=best_rec.get("slot"),
        raw=best_rec,
    )


async def execute_backrun(signal: BackrunSignal) -> str | None:
    """High-priority backrun via profitability-gated ``BackrunExecutor``."""
    from src.strategies.backrun_executor import get_backrun_executor

    logger.info(
        "Executing Helius backrun | mint=%s… amount_micro=%s sig=%s",
        signal.midcap_mint[:8],
        signal.amount_micro,
        signal.signature,
    )
    get_event_bus().publish_fire_and_forget(
        BotEvent(
            kind=EventKind.WEBHOOK_BACKRUN,
            source="helius",
            data={
                "mint": signal.midcap_mint,
                "amount_micro": signal.amount_micro,
                "signature": signal.signature,
            },
        )
    )
    victim_ctx = {
        "amount_micro": signal.amount_micro,
        "midcap_mint": signal.midcap_mint,
        "tx_sig": signal.signature,
        "signature": signal.signature,
    }
    success = await get_backrun_executor().execute(victim_ctx)
    return signal.signature if success else None


async def handle_helius_webhook_payload(payload: Any) -> dict[str, Any]:
    """Ingest all Jupiter SWAPs; schedule backrun when qualified."""
    count = ingest_helius_payload(payload)
    get_event_bus().publish_fire_and_forget(
        BotEvent(
            kind=EventKind.WEBHOOK_INGEST,
            source="helius",
            data={"records": count},
        )
    )

    if is_backrun_opportunity(payload):
        from src.strategies.brain_signals import note_backrun_context

        signal = parse_backrun_signal(payload)
        if signal is not None:
            note_backrun_context(
                {
                    "enabled": True,
                    "active": True,
                    "pipeline_active": True,
                    "amount_micro": signal.amount_micro,
                    "midcap_mint": signal.midcap_mint,
                    "tx_sig": signal.signature,
                }
            )
            asyncio.create_task(execute_backrun(signal))

    return {"status": "ok", "records": count}


@router.post("/helius/webhook", response_model=None)
async def helius_webhook(request: Request) -> dict[str, Any] | JSONResponse:
    raw = await request.body()
    ok, err = verify_helius_http_request(raw, request.headers)
    if not ok:
        return JSONResponse({"detail": err}, status_code=401)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"detail": "invalid json"}, status_code=400)

    return await handle_helius_webhook_payload(data)


@router.get("/helius/health")
async def helius_health() -> dict[str, str]:
    return {"status": "ok"}
