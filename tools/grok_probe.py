#!/usr/bin/env python3
"""Probe SuperGrok's weekly pool — the diagnostic behind the Grok row.

`glideslope.py` reports Grok through the same tables as every other provider.
When that row goes missing or reads wrong, this is the tool that shows why: it
makes the billing GET (or reads a saved payload), prints the raw JSON, and
prints Glideslope's normalization beside it.

A live call may refresh the Grok Build access token in ~/.grok/auth.json, the
same operation Grok itself runs. The token is never printed.

Usage:
    python3 tools/grok_probe.py              # raw payload + normalized windows
    python3 tools/grok_probe.py --raw        # raw payload only (pipe to jq)
    python3 tools/grok_probe.py --save FILE  # keep the payload as a test fixture
    python3 tools/grok_probe.py --payload FILE
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
    parser = argparse.ArgumentParser(description="Probe SuperGrok's weekly usage pool.")
    parser.add_argument("--raw", action="store_true", help="print the raw payload only")
    parser.add_argument("--save", type=Path, help="write the raw payload to a file (test fixture)")
    parser.add_argument("--payload", type=Path, help="normalize a saved payload instead of calling out")
    parser.add_argument("--force-live", action="store_true",
                        help="skip the Stop-hook snapshot and hit billing")
    args = parser.parse_args(argv)

    try:
        payload = (json.loads(args.payload.read_text()) if args.payload
                   else glideslope.query_grok(force_live=args.force_live))
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
        position = glideslope.normalize_grok(payload, now)
    except glideslope.PositionError as exc:
        print(f"\n✗ normalization rejected the payload: {exc}", file=sys.stderr)
        return 1

    print("\n── as Glideslope reads it ──────────────────────────────────────")
    print(f"plan (operator-declared): {glideslope.GROK_PLAN}")
    print(f"plan_raw: {position['plan'] or '—'}")
    print()
    for limit in position["limits"]:
        used = limit["used_percent"]
        pace = glideslope.even_pace_percent(limit, now)
        mark = f"◆ {glideslope.format_percent(pace)}" if pace is not None else "◆ —"
        print(f"  {limit['label']:<22} {bar(used)}  {glideslope.format_percent(used):>5} used"
              f"   {mark:>10}   resets {glideslope.format_reset(limit, now)}")
    if position.get("product_usage"):
        print()
        for item in position["product_usage"]:
            print(f"  {item['product']:<22} {bar(item['used_percent'])}  "
                  f"{glideslope.format_percent(item['used_percent']):>5} of the weekly pool")
    print()
    print("  meter ids:", ", ".join(limit["meter_id"] for limit in position["limits"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
