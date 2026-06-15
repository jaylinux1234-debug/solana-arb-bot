# `src/` layout

```
src/
├── core/           # rpc, wallet, hot signer, circuit breaker, risk, sizing
├── cex/            # backpack, ccxt_wrapper, price_feed, inventory
├── dex/            # jupiter, phoenix, kamino, quotes
├── strategies/     # cex_dex, liquidation, collateral_swap, flash, brain
├── execution/      # jito, bundle, helius, mempool, flash (re-exports)
├── monitoring/     # metrics, alerts, health, logging, structlog
├── ai/             # decision_engine, models (ML), trainers
├── events/         # event bus + webhook ingest
├── utils/          # price, redis, trade_logger, helpers
├── config/         # settings (non-secret env)
└── main.py         # entrypoint
```

## Import conventions

| Prefer | Legacy shim (still works) |
|--------|---------------------------|
| `src.events.webhook.helius` | `src.webhook.helius` |
| `src.monitoring.alerts` | `src.utils.alerts` |
| `src.events.bus` | — |
| `src.cex.ccxt_wrapper` | via `price_feed` |
| `src.execution.flash` | `src.strategies.*flash*` |
| `src.ai.models` | direct `src.ai.*` / `strategies.ml_filter` |

`config/` and `scripts/` under `src/` stay as tooling; production bot code uses the packages above.
