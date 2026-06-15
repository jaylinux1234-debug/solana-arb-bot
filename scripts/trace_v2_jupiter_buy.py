#!/usr/bin/env python3
"""Step-by-step trace of v2 Jupiter USDC→SOL buy (no CEX sell)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
)

logger = logging.getLogger("trace_v2_jupiter")


async def main() -> int:
    dry_run = os.getenv("TRACE_JUPITER_DRY_RUN", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    size_usdc = float(os.getenv("TRACE_SIZE_USDC", "10"))

    from src.config.settings import bootstrap_config
    from src.cex.backpack import BackpackClient
    from src.core.risk import RiskEngine
    from src.core.wallet import initialize_wallet
    from src.dex.jupiter import JupiterClient
    from src.strategies.dex_cex_reverse import DexCexReverseStrategy
    from src.v2.config import V2Config
    from src.v2.dex_cex_reverse import V2ReverseLane

    bootstrap_config()
    cfg = V2Config.from_env()
    cfg.apply_reverse_env()
    settings = bootstrap_config()
    await initialize_wallet()

    risk = RiskEngine(settings)
    backpack = BackpackClient(settings)
    jupiter = JupiterClient(settings)
    wallet = settings.wallet_pubkey or settings.WALLET_PUBKEY or ""
    reverse = DexCexReverseStrategy(
        jupiter_executor=jupiter,
        backpack_client=backpack,
        wallet_pubkey=wallet,
        settings=settings,
        risk=risk,
    )
    lane = V2ReverseLane(reverse, cfg)

    size_micro = int(size_usdc * 1_000_000)
    signal = {
        "size_usdc": size_usdc,
        "size_usdc_micro": size_micro,
        "gross_bps": 15.0,
        "net_bps": 5.0,
        "direction": "dex_cheap",
    }

    report: dict = {
        "wallet": wallet[:12] + "…" if wallet else None,
        "ledger_sign_url": (os.getenv("LEDGER_SIGN_URL") or "")[:60],
        "dry_run": dry_run,
        "size_usdc": size_usdc,
        "slippage_bps": lane._get_slippage_bps(),
        "steps": [],
    }

    def step(name: str, ok: bool, detail: dict) -> None:
        report["steps"].append({"step": name, "ok": ok, **detail})
        status = "OK" if ok else "FAIL"
        logger.info("TRACE %s | %s | %s", name, status, json.dumps(detail, default=str)[:500])

    # 1 Preflight
    ok, fail = await lane._preflight_checks()
    if not ok:
        step("preflight", False, fail or {})
        print(json.dumps(report, indent=2, default=str))
        return 1
    step("preflight", True, {"onchain_usdc": await lane.usdc_manager.get_available_usdc()})

    # 2 Build swap
    built = await lane._build_jupiter_swap(size_micro, slippage_bps=lane._get_slippage_bps())
    if not built.get("success"):
        step("build_swap", False, built)
        print(json.dumps(report, indent=2, default=str))
        return 1
    step(
        "build_swap",
        True,
        {
            "out_lamports": built.get("out_lamports"),
            "has_swap_tx": bool((built.get("swap_data") or {}).get("swapTransaction")),
        },
    )

    if dry_run:
        report["stopped"] = "dry_run_before_sign"
        print(json.dumps(report, indent=2, default=str))
        return 0

    # 3 Sign
    signed_b64, sign_fail = await lane._sign_transaction(built)
    if not signed_b64:
        step("sign", False, sign_fail or {})
        print(json.dumps(report, indent=2, default=str))
        return 1
    step("sign", True, {"signed_len": len(signed_b64)})

    # 4 Jito send
    send = await lane._send_with_jito(
        signed_b64,
        amount_micro=size_micro,
        net_bps=float(signal["net_bps"]),
        gross_bps=float(signal["gross_bps"]),
    )
    if not send.get("success"):
        step("jito_send", False, send)
        print(json.dumps(report, indent=2, default=str))
        return 1
    step(
        "jito_send",
        True,
        {
            "tx_sig": send.get("tx_sig") or send.get("txid"),
            "tip_lamports": send.get("tip_lamports"),
        },
    )

    print(json.dumps(report, indent=2, default=str))
    await jupiter.close()
    await backpack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
