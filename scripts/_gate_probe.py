import asyncio
import os
from src.config.settings import get_settings
from src.cex.backpack import get_backpack_client
from src.dex.jupiter import JupiterClient

async def main():
    s = get_settings()
    bp = get_backpack_client(s)
    jup = JupiterClient(s)
    cex = await bp.get_sol_usdc_price()
    default_probe = int(s.trading.min_flash_usdc * 1_000_000 // 2)
    probe = int(os.getenv("CEX_DEX_PROBE_USDC_MICRO", str(default_probe)))
    q = await jup.get_quote_dict(amount=probe)
    await bp.close()
    await jup.close()
    mg = int(s.trading.cex_dex_min_gross_spread_bps)
    mn = int(s.trading.cex_dex_min_net_spread_bps)
    cost = int(s.trading.cex_dex_strategy_base_cost_bps)
    ai = float(s.trading.ai_approve_min_confidence)
    print("=== live gate snapshot ===")
    print("min_gross_bps:", mg, "min_net_bps:", mn, "base_cost_bps:", cost, "ai_conf:", ai)
    print("probe_micro:", probe, "usd:", probe/1e6)
    if not cex:
        print("BLOCK: Backpack price unavailable")
        return
    if not q:
        print("BLOCK: Jupiter quote failed")
        return
    jup_p = float(q["price"])
    gross = int((cex - jup_p) / jup_p * 10000)
    net = gross - cost
    conf = min(98.0, 65.0 + min(25.0, net * 0.6) + min(10.0, probe / 1_000_000.0))
    print("cex:", round(cex, 4), "jup:", round(jup_p, 4))
    print("gross_bps:", gross, "need >=", mg)
    print("net_bps:", net, "need >=", mn)
    print("confidence:", round(conf, 1), "need >=", ai)
    print("min gross for net gate:", mn + cost, "bps")
    fails = []
    if gross < mg: fails.append("gross too low")
    if net < mn: fails.append("net too low")
    if conf < ai: fails.append("confidence too low")
    print("RESULT:", "NO TRADE — " + "; ".join(fails) if fails else "OPPORTUNITY would fire")

asyncio.run(main())
