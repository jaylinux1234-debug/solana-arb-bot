#!/usr/bin/env python3
"""Docker HEALTHCHECK: HTTP GET /health (Solana bot liveness)."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

HEALTH_URL = "http://127.0.0.1:8000/health"


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            if 200 <= resp.status < 300:
                print(f"docker_healthcheck: ok {HEALTH_URL}")
                return 0
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"docker_healthcheck: FAIL {exc}", file=sys.stderr)
        return 1
    print(f"docker_healthcheck: FAIL unexpected status", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
