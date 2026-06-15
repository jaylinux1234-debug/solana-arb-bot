"""CCXT exchange factories (discovery venues; orders stay on Backpack)."""

from __future__ import annotations

import os
from typing import Any

import ccxt


def _rate_limit_config(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"enableRateLimit": True}
    if extra:
        cfg.update(extra)
    return cfg


def create_backpack_exchange() -> ccxt.Exchange:
    """Backpack — primary CEX (API key from env when set)."""
    return ccxt.backpack(
        _rate_limit_config(
            {
                "apiKey": os.getenv("BACKPACK_API_KEY"),
                "secret": os.getenv("BACKPACK_SECRET"),
            }
        )
    )


def create_bybit_exchange() -> ccxt.Exchange:
    return ccxt.bybit(_rate_limit_config())


def create_okx_exchange() -> ccxt.Exchange:
    return ccxt.okx(_rate_limit_config())


def create_kucoin_exchange() -> ccxt.Exchange:
    return ccxt.kucoin(_rate_limit_config())


def discovery_venue_factories(
    *,
    skip_venues: set[str] | None = None,
) -> list[tuple[str, Any]]:
    """Ordered (name, factory) pairs for price discovery."""
    skip = skip_venues or set()
    venues = [
        ("backpack", create_backpack_exchange),
        ("bybit", create_bybit_exchange),
        ("okx", create_okx_exchange),
        ("kucoin", create_kucoin_exchange),
    ]
    return [(n, f) for n, f in venues if n not in skip]
