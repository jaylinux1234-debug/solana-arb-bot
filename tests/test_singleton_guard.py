"""Tests for next-level singleton guard (same-pid stale reclaim)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.core import singleton_guard


def test_reclaim_stale_lock_same_pid_stale_token() -> None:
    client = MagicMock()
    client.exists.return_value = True
    client.get.return_value = "4242:deadbeef"
    with patch.object(singleton_guard, "pid_alive", return_value=True):
        with patch.object(singleton_guard.os, "getpid", return_value=4242):
            reclaimed = singleton_guard.reclaim_stale_lock(
                client,
                "bot:singleton:v2",
                our_token=None,
            )
    assert reclaimed is True
    client.delete.assert_called_once_with("bot:singleton:v2")


def test_reclaim_stale_lock_same_pid_matching_token_not_reclaimed() -> None:
    client = MagicMock()
    client.exists.return_value = True
    client.get.return_value = "4242:deadbeef"
    with patch.object(singleton_guard, "pid_alive", return_value=True):
        with patch.object(singleton_guard.os, "getpid", return_value=4242):
            reclaimed = singleton_guard.reclaim_stale_lock(
                client,
                "bot:singleton:v2",
                our_token="4242:deadbeef",
            )
    assert reclaimed is False
    client.delete.assert_not_called()


def test_acquire_with_stealing_reclaims_same_pid_without_wait() -> None:
    client = MagicMock()
    client.exists.side_effect = [True, False]
    client.get.return_value = "1:oldtoken"
    client.set.return_value = True

    with patch.object(singleton_guard, "redis_url", return_value="redis://localhost:6379/0"):
        with patch("redis.from_url", return_value=client):
            with patch.object(singleton_guard, "pid_alive", return_value=True):
                with patch.object(singleton_guard.os, "getpid", return_value=1):
                    with patch.object(singleton_guard, "_start_renew"):
                        ok = singleton_guard.acquire_with_stealing(key="bot:singleton:v2")
    assert ok is True
    client.delete.assert_called_with("bot:singleton:v2")
