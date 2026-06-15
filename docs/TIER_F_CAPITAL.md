# Tier F — Capital alignment (SOL + USDC)

Run after Plans 1–8 are deployed. Goal: fund **both** directions (CEX→DEX needs CEX USDC / inventory SOL; DEX→CEX reverse needs **on-chain USDC**).

## Prerequisites

| Item | Target |
|------|--------|
| Backpack | USDC for CEX buys + SOL inventory policy as configured |
| On-chain (Ledger wallet) | SOL for fees + inventory sells; **USDC** for reverse lane |
| `.env` | `ENABLE_DEX_CEX_REVERSE=true`, `CEX_DEX_INVENTORY_FIRST=true` |
| Host | Ledger bridge on `:8546`, Docker up |

Suggested split for a ~$15–150 small account (adjust to taste):

- **~60–70%** notional as SOL on-chain (inventory-first `cex_dex`)
- **~30–40%** as USDC on-chain (reverse / Jupiter buys)
- Keep **$5–20 USDC on Backpack** if you use full CEX-buy path

## Recommended execution order

### Step 0 — Fund wallets

1. Deposit USDC + SOL to **Backpack** (CEX leg).
2. Withdraw / hold on **Ledger pubkey**: SOL + USDC SPL (mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`).
3. Confirm in logs after deploy: `Wallet initialized | USDC: $… | SOL_chain=…`

### Step 1 — Baseline metrics (before tuning)

```bash
npm run metrics:next
npm run probe:daily
npm run sim:breakeven:live
```

Saves a snapshot under `logs/` (trade history + auto-tune output when fills exist).

**Windows:**

```powershell
npm run metrics:next
npm run probe:daily
npm run sim:breakeven:live
```

### Step 2 — Deploy / refresh prod

```bash
npm run sync:compose-env
npm run secrets:sync-local
npm run go-live:preflight:sh
npm run ledger:bridge          # separate terminal — host only
npm run compose:prod:up
```

**Windows:** same; use `npm run deploy:next-level:ps1` instead of manual compose if preferred.

### Step 3 — Auto gates + 2h monitor

Terminal A (auto gate switcher — strict ↔ opportunistic on vol):

```bash
npm run gates:auto
```

Terminal B (enhanced monitor):

```bash
npm run monitor:enhanced
```

Optional: `npm run logs:tail` in a third terminal.

Let run **~2 hours**. Watch for `CEX_CHEAP` lines and `wrong_direction_dex_cheap` in near-misses.

### Step 4 — Direction fix (if >3 direction near-misses / 2h)

In `.env`:

```env
ENABLE_DEX_CEX_REVERSE=true
ENABLE_REVERSE_USDC_BOOTSTRAP=true
REVERSE_BOOTSTRAP_MIN_LIVE_FILLS=0
USDC_INVENTORY_TARGET_PCT=30
```

If SOL-heavy and reverse still starved (no on-chain USDC):

```bash
npm run inventory:usdc-sync:dry    # preview
npm run inventory:usdc-sync        # live swap (requires signing)
```

Then:

```bash
npm run sync:compose-env
npm run compose:prod:restart:no-build
```

### Step 5 — After first `live_fill`

```bash
npm run train:ml:real-fills
python scripts/gate_modes.py strict
npm run sync:compose-env
npm run compose:prod:restart:no-build
npm run tune:auto
```

Re-tighten gates (`strict` profile); only use `first-fill` mode for short experiments.

## Copy-paste implementation checklist

`scripts/next_level_metrics.py` and `metrics:next` already exist in this repo — **do not** overwrite unless you intend to reset them.

```bash
# From repo root
npm run metrics:next
npm run sync:compose-env
npm run compose:prod:up
```

## npm scripts reference

| Script | Purpose |
|--------|---------|
| `npm run metrics:next` | Baseline KPIs (fills, blocks, hours-to-first-fill) |
| `npm run gates:auto` | Vol-triggered strict ↔ opportunistic |
| `npm run monitor:enhanced` | Live `cex_cheap` + block histogram |
| `npm run inventory:usdc-sync` | Plan 9 SOL→USDC top-up |
| `npm run train:ml:real-fills` | Post-fill ML |
| `npm run tune:auto` | Plan 10 cost/gate suggestions |

## Success signals

- Near-miss mix shifts from mostly `wrong_direction_dex_cheap` to `env_thresholds` / `cex_cheap`
- `npm run metrics:next` shows `live_fills >= 1`
- Brain logs alternate `executing_lane=cex_dex` and `dex_cex_reverse` when market flips
