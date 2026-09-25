#!/usr/bin/env python3
"""Keep a stalled Kimi Code agent fed until the week's ration is actually spent.

Kimi's coding plan rations two windows at once: a rolling 5h burst and a 7-day plan
quota. A swarm that outruns the burst window dies on a `403 usage limit` and sits
there — the pane looks alive, the agent is dead, and nothing restarts it. This walks
the agent back up whenever the window has refilled.

It is a **poller, not a scheduler**. There is no interval arithmetic and no reset-time
phase to keep in sync: every run asks Kimi where we actually are and asks the session
log whether the agent is actually stalled. Wall-clock cadence therefore cannot drift
out of phase with the window, and a session started at any hour is picked up on the
next tick.

A nudge fires only when ALL of these hold:

  1. the 7-day plan quota is below --weekly-floor   (else the week is spent; stand down)
  2. the 5h burst window is below --session-floor   (else the wall is still up)
  3. no LLM call has been logged for --stall-minutes (else it is working, leave it be)
  4. the last nudge was more than --cooldown ago    (else we already spoke)

Position comes from Kimi itself via glideslope.query_kimi/normalize_kimi — the
authoritative normalized quota units, never a token-based estimate of our own.
Liveness comes from the mtime of the newest `wire.jsonl` under ~/.kimi-code/sessions,
which the harness appends to on every LLM call.

A stalled-but-finished agent is indistinguishable from a stalled-but-blocked one from
the outside, so a finished agent will get a `continue` too. That is the intended
trade: this exists to burn a ration that otherwise expires unused.

The agent is reached through a terminal multiplexer's CLI: c11, a macOS multiplexer,
expected at the path in C11_BIN. It is used to find the pane (`tree`), read its screen
(`read-screen`) and type into it (`send`); another multiplexer would need those three.

**Surface refs are recycled.** The multiplexer renumbers them wholesale across restarts,
so the ref that named the Kimi pane yesterday can name an unrelated shell today —
verified in the wild 2026-08-06, when every ref in the window shifted between morning
and afternoon.
Before typing anything, the target's screen must match --expect. This is a heuristic,
not proof: it reads the TUI chrome, so a pane that merely *mentions* Kimi can pass. It
is aimed squarely at the real failure — a fresh shell inheriting the number — and a
shell shows a prompt, not a model name.

Usage:
    python3 tools/kimi_resume.py --surface surface:200            # one decision + act
    python3 tools/kimi_resume.py --surface surface:200 --dry-run  # decide, print, send nothing
    python3 tools/kimi_resume.py --surface surface:200 --status   # position + liveness only

Install as a 5-minute poller with tools/install-kimi-resume.sh.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import glideslope  # noqa: E402  — path is set above

C11_BIN = Path("/Applications/c11.app/Contents/Resources/bin/c11")
SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"
STATE_PATH = glideslope.STORE_DIR / "kimi-resume.state.json"
LOG_PATH = glideslope.STORE_DIR / "kimi-resume.log"

# Kimi reports 100 == exhausted. Leave a sliver so a window that is a rounding error
# away from full is not treated as usable.
DEFAULT_SESSION_FLOOR = 98.0
DEFAULT_WEEKLY_FLOOR = 99.0

# The pane's screen must match this before anything is typed into it. Kimi's TUI keeps
# its model in the persistent chrome ("K3-256k" in the header and the footer), so a
# match survives scrollback. Refs get recycled; this is what stops a `continue` from
# landing in whatever shell inherited the number.
EXPECT_PATTERN = r"(?i)kimi|k[0-9]+-[0-9]+k"


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_PATH.open("a") as handle:
        handle.write(f"{stamp} {message}\n")


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)


def newest_wire_mtime(sessions_dir: Path) -> tuple[dt.datetime | None, Path | None]:
    """Most recent LLM-call append across every agent of every session."""
    newest_ts, newest_path = None, None
    for wire in sessions_dir.rglob("wire.jsonl"):
        try:
            ts = wire.stat().st_mtime
        except OSError:
            continue
        if newest_ts is None or ts > newest_ts:
            newest_ts, newest_path = ts, wire
    if newest_ts is None:
        return None, None
    return dt.datetime.fromtimestamp(newest_ts, dt.timezone.utc), newest_path


def limit_by_meter(position: dict, meter_id: str) -> dict | None:
    for limit in position.get("limits", []):
        if limit.get("meter_id") == meter_id:
            return limit
    return None


def resolve_surface(surface: str) -> str | None:
    """The workspace ref containing this surface, or None if it does not exist.

    The multiplexer recycles surface refs — they renumber wholesale across restarts, so a ref that
    named the Kimi pane yesterday can name an unrelated shell today. Resolution is a
    live lookup on every tick, never a cached assumption, and the workspace ref it
    returns is what read-screen needs (a bare surface ref resolves only within the
    caller's workspace, which launchd does not have).
    """
    try:
        out = subprocess.run([str(C11_BIN), "tree", "--all", "--json"],
                             capture_output=True, text=True, timeout=15)
        tree = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for window in tree.get("windows") or []:
        for workspace in window.get("workspaces") or []:
            for pane in workspace.get("panes") or []:
                for found in pane.get("surfaces") or []:
                    if found.get("ref") == surface:
                        return workspace.get("ref")
    return None


def read_screen(workspace: str, surface: str, lines: int = 60) -> str:
    try:
        out = subprocess.run([str(C11_BIN), "read-screen", "--workspace", workspace,
                              "--surface", surface, "--lines", str(lines)],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout


def send_continue(surface: str, text: str = "continue") -> tuple[bool, str]:
    try:
        out = subprocess.run([str(C11_BIN), "send", "--surface", surface, text],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    ok = out.returncode == 0
    return ok, (out.stdout or out.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nudge a stalled Kimi Code agent when its window refills.")
    parser.add_argument("--surface", required=True, help="c11 surface ref of the Kimi pane, e.g. surface:200")
    parser.add_argument("--text", default="continue", help="what to send (default: continue)")
    parser.add_argument("--stall-minutes", type=float, default=10.0,
                        help="minutes with no LLM call before the agent counts as stalled")
    parser.add_argument("--cooldown", type=float, default=15.0, help="minutes between nudges")
    parser.add_argument("--session-floor", type=float, default=DEFAULT_SESSION_FLOOR,
                        help="5h window must be below this percent to nudge")
    parser.add_argument("--weekly-floor", type=float, default=DEFAULT_WEEKLY_FLOOR,
                        help="7-day quota must be below this percent to nudge")
    parser.add_argument("--sessions-dir", type=Path, default=SESSIONS_DIR)
    parser.add_argument("--expect", default=EXPECT_PATTERN,
                        help="regex the pane's screen must match before sending "
                             f"(default {EXPECT_PATTERN!r}); pass '' to disable")
    parser.add_argument("--dry-run", action="store_true", help="decide and report, send nothing")
    parser.add_argument("--status", action="store_true", help="print position and liveness, then exit")
    args = parser.parse_args(argv)

    now = glideslope.utc_now()

    try:
        position = glideslope.normalize_kimi(glideslope.query_kimi(), now)
    except glideslope.PositionError as exc:
        log(f"SKIP position unavailable: {exc}")
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    session = limit_by_meter(position, "session")
    weekly = limit_by_meter(position, "weekly_all")
    if session is None or weekly is None:
        log("SKIP position missing session/weekly meter")
        print("✗ Kimi position has no session/weekly_all meter", file=sys.stderr)
        return 1

    last_call, wire = newest_wire_mtime(args.sessions_dir)
    idle_minutes = (now - last_call).total_seconds() / 60 if last_call else None

    if args.status:
        print(f"5h session   {session['used_percent']:5.1f}%   resets {glideslope.format_reset(session, now)}")
        print(f"weekly quota {weekly['used_percent']:5.1f}%   resets {glideslope.format_reset(weekly, now)}")
        print(f"last LLM call: {f'{idle_minutes:.1f} min ago' if idle_minutes is not None else 'never (no wire.jsonl)'}")
        if wire:
            print(f"  {wire}")
        return 0

    # --- the four gates -------------------------------------------------------
    if weekly["used_percent"] >= args.weekly_floor:
        log(f"STAND DOWN weekly {weekly['used_percent']:.1f}% >= {args.weekly_floor} "
            f"(resets {glideslope.format_reset(weekly, now)})")
        return 0

    if session["used_percent"] >= args.session_floor:
        log(f"WAIT 5h window {session['used_percent']:.1f}% used "
            f"(resets {glideslope.format_reset(session, now)})")
        return 0

    if idle_minutes is None:
        log("SKIP no wire.jsonl found — nothing to resume")
        return 0

    if idle_minutes < args.stall_minutes:
        log(f"BUSY last call {idle_minutes:.1f} min ago (< {args.stall_minutes})")
        return 0

    state = read_state()
    last_sent = glideslope.parse_timestamp(state.get("last_sent_at"))
    if last_sent is not None:
        since = (now - last_sent).total_seconds() / 60
        if since < args.cooldown:
            log(f"COOLDOWN last nudge {since:.1f} min ago (< {args.cooldown})")
            return 0

    reason = (f"5h {session['used_percent']:.1f}% · weekly {weekly['used_percent']:.1f}% · "
              f"idle {idle_minutes:.1f} min")

    workspace = resolve_surface(args.surface)
    if workspace is None:
        log(f"ERROR {args.surface} not found — not sending")
        print(f"✗ {args.surface} not found", file=sys.stderr)
        return 1

    # A ref that exists is not a ref that is still the Kimi pane. Typing `continue`
    # into a shell that inherited the number is the failure this guards.
    if args.expect:
        screen = read_screen(workspace, args.surface)
        if not re.search(args.expect, screen):
            log(f"ERROR {args.surface} does not look like a Kimi pane "
                f"(no /{args.expect}/ on screen) — not sending")
            print(f"✗ {args.surface} is not a Kimi pane", file=sys.stderr)
            return 1

    if args.dry_run:
        log(f"DRY-RUN would send {args.text!r} to {args.surface} — {reason}")
        print(f"would send {args.text!r} to {args.surface} (in {workspace})\n  {reason}")
        return 0

    ok, detail = send_continue(args.surface, args.text)
    if not ok:
        log(f"ERROR send failed: {detail}")
        print(f"✗ send failed: {detail}", file=sys.stderr)
        return 1

    state["last_sent_at"] = glideslope.iso_utc(now)
    state["last_reason"] = reason
    state["surface"] = args.surface
    write_state(state)
    log(f"SENT {args.text!r} -> {args.surface} — {reason}")
    print(f"sent {args.text!r} to {args.surface} ({reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
