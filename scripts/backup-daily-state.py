#!/usr/bin/env python3
"""Daily snapshot: Redis bot keys + wallet safety JSON (pg_dump-style state backup)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _backup_dir(base: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    out = base / stamp
    out.mkdir(parents=True, exist_ok=True)
    return out


async def _dump_redis(out: Path) -> int:
    from src.utils.redis import get_redis

    r = await get_redis()
    if r is None:
        print("  Redis: REDIS_URL unset — skip")
        return 0

    prefix = (os.getenv("STATE_BACKUP_REDIS_PREFIX") or "bot:").strip()
    keys = []
    async for key in r.scan_iter(match=f"{prefix}*"):
        keys.append(key)
    if not keys:
        keys = [k async for k in r.scan_iter(match="*")]

    dump: dict[str, object] = {"_meta": {"prefix": prefix, "count": len(keys)}}
    for key in keys:
        t = await r.type(key)
        if t == "string":
            dump[key] = await r.get(key)
        elif t == "hash":
            dump[key] = await r.hgetall(key)
        elif t == "list":
            dump[key] = await r.lrange(key, 0, -1)
        elif t == "set":
            dump[key] = list(await r.smembers(key))
        else:
            dump[key] = {"_type": t}

    path = out / "redis_state.json"
    path.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(f"  Redis: {len(keys)} keys → {path}")
    return len(keys)


def _copy_wallet_files(out: Path) -> int:
    candidates = [
        os.getenv("WALLET_SAFETY_STATE_PATH", "logs/wallet_safety_state.json"),
        os.getenv("PNL_CONFIDENCE_STATE_PATH", "logs/pnl_confidence_window.json"),
        "logs/wallet_safety_state.json",
    ]
    n = 0
    for rel in candidates:
        p = ROOT / rel
        if not p.is_file():
            continue
        dest = out / p.name
        shutil.copy2(p, dest)
        print(f"  Copied {p} → {dest}")
        n += 1
    return n


async def main() -> int:
    load_dotenv(ROOT / ".env")
    base = Path(os.getenv("STATE_BACKUP_DIR", str(ROOT / "backtest_results" / "state_snapshots")))
    out = _backup_dir(base)
    print(f"=== state backup → {out} ===")
    await _dump_redis(out)
    _copy_wallet_files(out)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "redis_prefix": os.getenv("STATE_BACKUP_REDIS_PREFIX", "bot:"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("=== backup complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
