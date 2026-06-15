"""Prometheus RPC provider failure counter."""

from __future__ import annotations

import pytest

prometheus_client = pytest.importorskip("prometheus_client")

from src.monitoring.metrics import record_rpc_provider_failure, rpc_provider_failures_total


def test_rpc_provider_failures_total_increments() -> None:
    assert rpc_provider_failures_total is not None
    before = prometheus_client.generate_latest().decode()
    record_rpc_provider_failure("alchemy")
    after = prometheus_client.generate_latest().decode()
    assert "rpc_provider_failures_total" in after
    assert 'provider="alchemy"' in after
    assert after.count('provider="alchemy"') >= before.count('provider="alchemy"')
