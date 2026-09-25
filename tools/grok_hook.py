#!/usr/bin/env python3
"""Minimum Grok Build hook: snapshot SuperGrok usage on SessionStart and Stop.

Grok calls this with a JSON event on stdin. We discard it. A Stop hook's stdout
is parsed as a decision, so this script prints nothing on stdout, always exits
0, and writes a numbers-only billing snapshot for Glideslope's sampler to prefer
when it is under 90s old.

The live GET uses Grok's already-valid session (Grok is running if a hook fired).
Token refresh is the sampler's job, for when Build is closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glideslope  # noqa: E402  — path is set above


def main() -> int:
    try:
        sys.stdin.read()
    except OSError:
        pass
    try:
        now = glideslope.utc_now()
        payload = glideslope.query_grok(force_live=True, now=now)
        glideslope.write_grok_hook_snapshot(payload, now)
        glideslope.cache_provider_read("grok", payload, now)
    except Exception:  # noqa: BLE001 — a hook must never block a Grok turn
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
