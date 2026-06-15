"""Distributed singleton lock (Redis) with localhost port fallback."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_singleton_socket: socket.socket | None = None
_redis_client: object | None = None
_redis_lock_key: str | None = None
_redis_lock_token: str | None = None
_renew_thread: threading.Thread | None = None
_renew_stop = threading.Event()


def _lock_mode() -> str:
    raw = (os.getenv("BOT_SINGLETON_LOCK_MODE") or "auto").strip().lower()
    if raw in ("redis", "port", "auto"):
        return raw
    return "auto"


def _redis_url() -> str:
    return (os.getenv("REDIS_URL") or "").strip()


def _lock_key() -> str:
    bot_id = (os.getenv("BOT_SINGLETON_ID") or os.getenv("HOSTNAME") or "solana-arb-bot").strip()
    return f"bot:singleton:{bot_id}"


def _lock_ttl_sec() -> int:
    try:
        return max(30, int(os.getenv("BOT_SINGLETON_LOCK_TTL_SEC", "120")))
    except ValueError:
        return 120


def _lock_wait_sec() -> int:
    """Seconds to retry Redis lock acquisition (e.g. after container recreate)."""
    try:
        return max(0, int(os.getenv("BOT_SINGLETON_LOCK_WAIT_SEC", "150")))
    except ValueError:
        return 150


def _acquire_port_lock(*, log: logging.Logger) -> None:
    global _singleton_socket
    if os.getenv("DISABLE_BOT_SINGLETON_LOCK", "").lower() in ("1", "true", "yes"):
        log.warning("DISABLE_BOT_SINGLETON_LOCK is set — multiple instances allowed.")
        return

    port = int(os.getenv("BOT_SINGLETON_LOCK_PORT", "38471"))
    host = (os.getenv("BOT_SINGLETON_LOCK_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen(1)
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"Another bot instance is already running (port lock {host}:{port}). "
            f"Stop the other process or set BOT_SINGLETON_LOCK_MODE=redis. Error: {exc}"
        ) from None
    _singleton_socket = sock
    log.info("Singleton port lock on %s:%s", host, port)


def _renew_loop() -> None:
    ttl = _lock_ttl_sec()
    interval = max(10, ttl // 3)
    while not _renew_stop.wait(interval):
        if _redis_client is None or _redis_lock_key is None or _redis_lock_token is None:
            continue
        try:
            current = _redis_client.get(_redis_lock_key)
            if current != _redis_lock_token:
                logger.error("Singleton Redis lock lost (token mismatch)")
                break
            _redis_client.expire(_redis_lock_key, ttl)
        except Exception as exc:
            logger.warning("Singleton lock renew failed: %s", exc)


def _acquire_redis_lock(*, log: logging.Logger) -> None:
    global _redis_client, _redis_lock_key, _redis_lock_token, _renew_thread

    url = _redis_url()
    if not url:
        raise RuntimeError("BOT_SINGLETON_LOCK_MODE=redis requires REDIS_URL")

    try:
        import redis
    except ImportError as exc:
        raise SystemExit("redis package required for Redis singleton lock") from exc

    from src.core.singleton_guard import reclaim_stale_lock, terminate_pid

    client = redis.from_url(url, decode_responses=True)
    key = _lock_key()
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    ttl = _lock_ttl_sec()

    reclaim_stale_lock(client, key, log=log)
    if client.exists(key) and os.getenv("BOT_SINGLETON_TERMINATE_HOLDER", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        from src.core.singleton_guard import parse_holder_pid

        holder = client.get(key)
        pid = parse_holder_pid(holder)
        if pid is not None and pid != os.getpid():
            terminate_pid(pid, log=log)
            time.sleep(max(1, int(os.getenv("BOT_SINGLETON_TERMINATE_WAIT_SEC", "3") or 3)))
            reclaim_stale_lock(client, key, log=log)

    wait_sec = _lock_wait_sec()
    retry_interval = max(2, min(10, ttl // 6))
    deadline = time.monotonic() + wait_sec
    while not client.set(key, token, nx=True, ex=ttl):
        reclaim_stale_lock(client, key, log=log)
        holder = client.get(key)
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"Another bot holds the Redis singleton lock ({key}). Holder={holder!r}. "
                "Stop the other instance or wait for TTL expiry."
            )
        log.info(
            "Waiting for singleton lock %s (holder=%r, retry in %ss)",
            key,
            holder,
            retry_interval,
        )
        time.sleep(retry_interval)

    _redis_client = client
    _redis_lock_key = key
    _redis_lock_token = token
    _renew_stop.clear()
    _renew_thread = threading.Thread(target=_renew_loop, name="singleton-lock-renew", daemon=True)
    _renew_thread.start()
    log.info("Singleton Redis lock acquired | key=%s ttl=%ss", key, ttl)


def acquire_bot_singleton_lock(*, logger: logging.Logger | None = None) -> None:
    """
    Ensure a single bot leader per deployment.

    - ``BOT_SINGLETON_LOCK_MODE=redis`` — Redis SET NX (distributed).
    - ``port`` — localhost TCP bind (single host).
    - ``auto`` — Redis when ``REDIS_URL`` is set, else port.
    """
    log = logger or logging.getLogger(__name__)
    mode = _lock_mode()
    if mode == "auto":
        mode = "redis" if _redis_url() else "port"

    if mode == "redis":
        try:
            _acquire_redis_lock(log=log)
            return
        except SystemExit:
            raise
        except Exception as exc:
            log.warning("Redis singleton lock failed (%s); falling back to port lock", exc)
            _acquire_port_lock(log=log)
            return

    _acquire_port_lock(log=log)


def release_bot_singleton_lock() -> None:
    global _singleton_socket, _redis_client, _redis_lock_key, _redis_lock_token, _renew_thread

    _renew_stop.set()
    if _renew_thread is not None and _renew_thread.is_alive():
        _renew_thread.join(timeout=2.0)
    _renew_thread = None

    if _redis_client is not None and _redis_lock_key and _redis_lock_token:
        try:
            pipe = _redis_client.pipeline()
            pipe.watch(_redis_lock_key)
            if _redis_client.get(_redis_lock_key) == _redis_lock_token:
                pipe.multi()
                pipe.delete(_redis_lock_key)
                pipe.execute()
        except Exception:
            try:
                if _redis_client.get(_redis_lock_key) == _redis_lock_token:
                    _redis_client.delete(_redis_lock_key)
            except Exception:
                pass
        try:
            _redis_client.close()
        except Exception:
            pass

    _redis_client = None
    _redis_lock_key = None
    _redis_lock_token = None

    if _singleton_socket is not None:
        try:
            _singleton_socket.close()
        except OSError:
            pass
        _singleton_socket = None
