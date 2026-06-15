#!/usr/bin/env python3
"""CLI for next-level Redis singleton guard (acquire / status / reclaim / release)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.singleton_guard import (  # noqa: E402
    acquire_nextlevel_singleton,
    acquire_with_stealing,
    lock_key,
    lock_status,
    reclaim_stale_lock,
    redis_url,
    release_nextlevel_singleton,
)


def _client():
    url = redis_url()
    if not url:
        raise SystemExit("REDIS_URL is not set")
    try:
        import redis
    except ImportError as exc:
        raise SystemExit("pip install redis") from exc
    return redis.from_url(url, decode_responses=True)


def cmd_status(_: argparse.Namespace) -> int:
    info = lock_status()
    for k, v in info.items():
        print(f"{k}: {v}")
    return 0 if info.get("ok") else 1


def cmd_reclaim(_: argparse.Namespace) -> int:
    client = _client()
    key = lock_key()
    cleared = reclaim_stale_lock(client, key)
    print(f"reclaimed={cleared} key={key}")
    client.close()
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    if args.block:
        acquire_nextlevel_singleton()
        print(f"acquired {lock_key()}")
        return 0
    ok = acquire_with_stealing(terminate_live_holder=args.terminate)
    print("acquired" if ok else "failed")
    return 0 if ok else 1


def cmd_release(_: argparse.Namespace) -> int:
    release_nextlevel_singleton()
    print("released")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Next-level bot singleton guard")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show lock holder")
    p_status.set_defaults(func=cmd_status)

    p_reclaim = sub.add_parser("reclaim", help="Delete lock if holder PID is dead")
    p_reclaim.set_defaults(func=cmd_reclaim)

    p_acquire = sub.add_parser("acquire", help="Acquire lock (steal from dead PID)")
    p_acquire.add_argument(
        "--terminate",
        action="store_true",
        help="SIGTERM live holder when BOT_SINGLETON_TERMINATE_HOLDER not set",
    )
    p_acquire.add_argument(
        "--block",
        action="store_true",
        help="Retry until acquired (production bootstrap)",
    )
    p_acquire.set_defaults(func=cmd_acquire)

    p_release = sub.add_parser("release", help="Release lock held by this process")
    p_release.set_defaults(func=cmd_release)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
