"""Singleton lock mode selection."""

from __future__ import annotations

import pytest
from src.core import singleton_lock


def test_lock_mode_auto_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("BOT_SINGLETON_LOCK_MODE", "auto")
    assert singleton_lock._lock_mode() == "auto"
    assert singleton_lock._lock_mode() == "auto"
    # auto resolves to port when no REDIS_URL
    assert not singleton_lock._redis_url()


def test_lock_mode_redis_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_SINGLETON_LOCK_MODE", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    assert singleton_lock._lock_mode() == "redis"
