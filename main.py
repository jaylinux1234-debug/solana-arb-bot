#!/usr/bin/env python3
"""Project entrypoint — prefer ``python -m src.main``."""

from src.config.settings import bootstrap_config

bootstrap_config()

from src.main import run  # noqa: E402

if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
