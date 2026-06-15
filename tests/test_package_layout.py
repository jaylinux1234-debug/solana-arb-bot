"""Smoke test: src package layout exists."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_src_tree():
    expected = [
        "src/main.py",
        "src/config/settings.py",
        "src/core/security.py",
        "src/cex/executor.py",
        "src/cex/price_feed.py",
        "src/dex/jupiter.py",
        "src/strategies/cex_dex_cycle.py",
        "src/execution/jito.py",
        "src/utils/redis.py",
        "src/monitoring/health_server.py",
        "src/scripts/cex_dex_sim_batch.py",
    ]
    for rel in expected:
        assert (ROOT / rel).is_file(), rel
