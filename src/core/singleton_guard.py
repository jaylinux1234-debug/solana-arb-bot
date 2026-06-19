"""
Next-level singleton guard: Redis lock with dead-PID reclaim and optional holder terminate.

Used by ``singleton_lock`` and ``scripts/singleton_guard.py``.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOCK_KEY = "bot:singleton:nextlevel"
DEFAULT_TTL_SEC = 300

_client: Any | None = None
_lock_key: str | None = None
_lock_token: str | None = None
_renew_thread: threading.Thread | None = None
_renew_stop = threading.Event()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def redis_url() -> str:
    return (os.getenv("REDIS_URL") or "").strip()


def lock_key() -> str:
    return (
        os.getenv("BOT_SINGLETON_NEXTLEVEL_KEY")
        or os.getenv("BOT_SINGLETON_LOCK_KEY")
        or DEFAULT_LOCK_KEY
    ).strip() or DEFAULT_LOCK_KEY


def lock_ttl_sec() -> int:
    for name in ("REDIS_CYCLE_TTL_SEC", "BOT_SINGLETON_NEXTLEVEL_TTL_SEC"):
        raw = os.getenv(name)
        if raw:
            try:
                return max(30, min(300, int(raw)))
            except ValueError:
                pass
    return DEFAULT_TTL_SEC


def renew_ttl_sec() -> int:
    raw = os.getenv("BOT_SINGLETON_RENEW_TTL_SEC")
    if raw:
        try:
            return max(30, min(120, int(raw)))
        except ValueError:
            pass
    return min(60, lock_ttl_sec())


def renew_interval_sec() -> int:
    raw = os.getenv("BOT_SINGLETON_RENEW_INTERVAL_SEC")
    if raw:
        try:
            return max(5, min(60, int(raw)))
        except ValueError:
            pass
    return max(5, renew_ttl_sec() // 3)


def parse_holder_pid(holder: str | bytes | None) -> int | None:
    """Extract PID from lock value (``12345`` or ``12345:uuid``)."""
    if holder is None:
        return None
    text = holder.decode() if isinstance(holder, bytes) else str(holder)
    text = text.strip()
    if not text:
        return None
    head = text.split(":", 1)[0]
    try:
        pid = int(head)
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def terminate_pid(pid: int, *, log: logging.Logger | None = None) -> bool:
    """Send SIGTERM to holder (best-effort; no-op when same PID)."""
    log = log or logger
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        log.warning("Sent terminate to singleton holder pid=%s", pid)
        return True
    except ImportError:
        pass
    except Exception as exc:
        log.debug("psutil terminate pid=%s failed: %s", pid, exc)
        return False

    if sys.platform == "win32":
        log.debug("SIGTERM skipped on Windows without psutil (pid=%s)", pid)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        log.warning("Sent SIGTERM to singleton holder pid=%s", pid)
        return True
    except OSError as exc:
        log.debug("SIGTERM pid=%s failed: %s", pid, exc)
        return False


def reclaim_stale_lock(
    client: Any,
    key: str,
    *,
    log: logging.Logger | None = None,
    our_token: str | None = None,
) -> bool:
    """Delete lock when holder PID is dead or same process with a stale token.

    Docker restarts often reuse pid=1; a leftover Redis token then looks like a
    live holder and blocks acquisition until TTL expiry.
    """
    log = log or logger
    if not client.exists(key):
        return False
    holder = client.get(key)
    pid = parse_holder_pid(holder)
    if pid is None:
        log.warning("Singleton lock %s has non-PID holder %r — not reclaiming", key, holder)
        return False
    if pid_alive(pid):
        if pid == os.getpid() and holder != our_token:
            client.delete(key)
            log.info(
                "Reclaimed singleton lock %s (same pid=%s, stale token)",
                key,
                pid,
            )
            return True
        return False
    client.delete(key)
    log.info("Reclaimed stale singleton lock %s (dead pid=%s)", key, pid)
    return True


def acquire_with_stealing(
    *,
    redis_url_override: str | None = None,
    key: str | None = None,
    ttl_sec: int | None = None,
    terminate_live_holder: bool | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """
    Acquire Redis singleton: reclaim dead PID, optionally SIGTERM live holder, then SET NX.

    Returns True when this process holds the lock.
    """
    global _client, _lock_key, _lock_token
    log = log or logger
    url = (redis_url_override or redis_url()).strip()
    if not url:
        raise RuntimeError("REDIS_URL is required for next-level singleton guard")

    try:
        import redis
    except ImportError as exc:
        raise SystemExit("redis package required for singleton guard") from exc

    client = redis.from_url(url, decode_responses=True)
    lock_k = key or lock_key()
    ttl = ttl_sec if ttl_sec is not None else lock_ttl_sec()
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    do_terminate = (
        terminate_live_holder
        if terminate_live_holder is not None
        else _env_bool("BOT_SINGLETON_TERMINATE_HOLDER", False)
    )

    if _lock_token is not None and _lock_key == lock_k:
        holder = client.get(lock_k)
        if holder == _lock_token:
            _start_renew(ttl)
            log.debug("Next-level singleton already held | key=%s", lock_k)
            return True

    reclaim_stale_lock(client, lock_k, log=log, our_token=_lock_token)

    if client.exists(lock_k):
        holder = client.get(lock_k)
        pid = parse_holder_pid(holder)
        if pid is not None and pid_alive(pid):
            if pid == os.getpid():
                log.info(
                    "Reclaiming singleton lock %s from same pid=%s before acquire",
                    lock_k,
                    pid,
                )
                client.delete(lock_k)
            elif do_terminate:
                log.warning(
                    "Another instance holds %s (pid=%s) — requesting shutdown",
                    lock_k,
                    pid,
                )
                terminate_pid(pid, log=log)
                time.sleep(max(1, int(os.getenv("BOT_SINGLETON_TERMINATE_WAIT_SEC", "3"))))
                reclaim_stale_lock(client, lock_k, log=log, our_token=_lock_token)
            else:
                log.info(
                    "Singleton lock held by live pid=%s (%s); waiting for TTL or terminate",
                    pid,
                    holder,
                )

    if client.set(lock_k, token, nx=True, ex=ttl):
        _client = client
        _lock_key = lock_k
        _lock_token = token
        _start_renew(ttl)
        log.info("Next-level singleton acquired | key=%s ttl=%ss pid=%s", lock_k, ttl, os.getpid())
        return True

    holder = client.get(lock_k)
    log.error("Failed to acquire singleton %s (holder=%r)", lock_k, holder)
    return False


def _renew_lock(ttl: int, renew_ex: int) -> bool:
    """Refresh or reclaim the Redis singleton lock (sync client)."""
    if _client is None or _lock_key is None or _lock_token is None:
        return False
    holder = _client.get(_lock_key)
    if holder == _lock_token:
        _client.set(_lock_key, _lock_token, ex=renew_ex)
        logger.debug("Singleton lock renewed")
        return True
    if holder is None:
        if _client.set(_lock_key, _lock_token, nx=True, ex=renew_ex):
            logger.warning("SINGLETON_LOCK_LOST — re-acquired after key expiry")
            return True
        logger.debug("Singleton key missing; re-acquire lost race")
        return False
    our_pid = os.getpid()
    holder_pid = parse_holder_pid(holder)
    if holder_pid == our_pid:
        _client.set(_lock_key, _lock_token, ex=renew_ex)
        logger.warning("SINGLETON_LOCK_LOST — reclaiming (same pid)")
        return True
    if holder_pid is not None and not pid_alive(holder_pid):
        reclaim_stale_lock(_client, _lock_key, log=logger)
        if _client.set(_lock_key, _lock_token, nx=True, ex=ttl):
            logger.warning("SINGLETON_LOCK_LOST — reclaimed stale holder pid=%s", holder_pid)
            return True
    logger.error(
        "Next-level singleton lock lost (token mismatch holder=%r)",
        holder,
    )
    return False


def _renew_loop(ttl: int) -> None:
    interval = renew_interval_sec()
    renew_ex = renew_ttl_sec()
    while not _renew_stop.wait(interval):
        try:
            if not _renew_lock(ttl, renew_ex):
                break
        except Exception as exc:
            logger.warning("Singleton guard renew failed: %s", exc)


def _start_renew(ttl: int) -> None:
    global _renew_thread
    _renew_stop.clear()
    _renew_thread = threading.Thread(
        target=_renew_loop,
        args=(ttl,),
        name="singleton-guard-renew",
        daemon=True,
    )
    _renew_thread.start()


def acquire_nextlevel_singleton(*, log: logging.Logger | None = None) -> None:
    """Block until lock acquired or raise SystemExit."""
    log = log or logger
    wait_sec = max(0, int(os.getenv("BOT_SINGLETON_LOCK_WAIT_SEC", "150") or 150))
    retry = max(2, min(10, lock_ttl_sec() // 6))
    deadline = time.monotonic() + wait_sec
    while not acquire_with_stealing(log=log):
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"Could not acquire singleton lock {lock_key()!r} within {wait_sec}s"
            )
        log.info("Retrying singleton acquire in %ss", retry)
        time.sleep(retry)


def release_nextlevel_singleton() -> None:
    """Release lock held by this process."""
    global _client, _lock_key, _lock_token, _renew_thread

    _renew_stop.set()
    if _renew_thread is not None and _renew_thread.is_alive():
        _renew_thread.join(timeout=2.0)
    _renew_thread = None

    if _client is not None and _lock_key and _lock_token:
        try:
            if _client.get(_lock_key) == _lock_token:
                _client.delete(_lock_key)
        except Exception:
            pass
        try:
            _client.close()
        except Exception:
            pass

    _client = None
    _lock_key = None
    _lock_token = None


def lock_status() -> dict[str, Any]:
    """Inspect current lock holder (for CLI / health)."""
    url = redis_url()
    if not url:
        return {"ok": False, "error": "REDIS_URL unset"}
    try:
        import redis
    except ImportError:
        return {"ok": False, "error": "redis package not installed"}

    key = lock_key()
    client = redis.from_url(url, decode_responses=True)
    try:
        holder = client.get(key)
        pid = parse_holder_pid(holder)
        return {
            "ok": True,
            "key": key,
            "holder": holder,
            "pid": pid,
            "pid_alive": pid_alive(pid) if pid else None,
            "our_pid": os.getpid(),
            "we_hold": holder == _lock_token if _lock_token else False,
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
