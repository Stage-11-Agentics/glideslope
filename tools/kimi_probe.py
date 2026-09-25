#!/usr/bin/env python3
"""Probe Kimi's coding-plan usage endpoint directly — the diagnostic behind the Kimi row.

`glideslope.py` reports Kimi through the same tables as every other provider. When
that row goes missing or reads wrong, this is the tool that shows why: it makes the
one GET, prints the raw JSON exactly as Kimi returned it, and prints Glideslope's
normalization of that same payload beside it. Which half is wrong tells you whether
the problem is upstream (endpoint, key, plan) or ours (normalize_kimi).

Read-only. One request per run. It reuses glideslope.query_kimi/normalize_kimi
rather than re-implementing them, so a drift in the contract cannot hide here.

Usage:
    python3 tools/kimi_probe.py              # raw payload + normalized windows
    python3 tools/kimi_probe.py --raw        # raw payload only (pipe to jq)
    python3 tools/kimi_probe.py --save FILE  # keep the payload as a test fixture

The key comes from KIMI_API_KEY / KIMI_CODING_API_KEY, else the configured keys_file
(default ~/.glideslope/keys.txt). It is never printed.

Endpoint contract, verified live 2026-07-27 — see PROVIDERS.md § Kimi:
    GET https://api.kimi.com/coding/v1/usages
    Authorization: Bearer sk-kimi-…            (static platform key, no OAuth)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glideslope  # noqa: E402  — path is set above


def bar(percent: float, width: int = 24) -> str:
    filled = round(percent / 100 * width)
    return "█" * filled + "·" * (width - filled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the Kimi coding-plan usage endpoint.")
    parser.add_argument("--raw", action="store_true", help="print the raw payload only")
    parser.add_argument("--save", type=Path, help="write the raw payload to a file (test fixture)")
    parser.add_argument("--payload", type=Path, help="normalize a saved payload instead of calling out")
    args = parser.parse_args(argv)

    try:
        payload = (json.loads(args.payload.read_text()) if args.payload
                   else glideslope.query_kimi())
    except glideslope.PositionError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"✗ could not read {args.payload}: {exc}", file=sys.stderr)
        return 1

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"saved raw payload → {args.save}", file=sys.stderr)

    if args.raw:
        print(json.dumps(payload, indent=2))
        return 0

    now = glideslope.utc_now()
    print("── raw ─────────────────────────────────────────────────────────")
    print(json.dumps(payload, indent=2))

    try:
        position = glideslope.normalize_kimi(payload, now)
    except glideslope.PositionError as exc:
        print(f"\n✗ normalization rejected the payload: {exc}", file=sys.stderr)
        return 1

    print("\n── as Glideslope reads it ──────────────────────────────────────")
    print(f"plan (raw membership level): {position['plan']}")
    if position.get("parallel_limit"):
        print(f"parallel cap: {position['parallel_limit']:.0f}")
    print()
    for limit in position["limits"]:
        used = limit["used_percent"]
        pace = glideslope.even_pace_percent(limit, now)
        mark = f"◆ {glideslope.format_percent(pace)}" if pace is not None else "◆ —"
        print(f"  {limit['label']:<22} {bar(used)}  {glideslope.format_percent(used):>5} used"
              f"   {mark:>10}   resets {glideslope.format_reset(limit, now)}")
    print()
    print("  meter ids:", ", ".join(limit["meter_id"] for limit in position["limits"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
