#!/usr/bin/env python3
"""Kick the Glideslope sampler the moment this machine's Claude logins change.

launchd runs this whenever a watched login file changes (tools/install-login-watch.sh):
~/.claude.json, each login home's .claude.json, and claude-account's selection. Those
files are rewritten constantly for unrelated reasons, so the work here is to decide
cheaply whether anything that matters changed — the set of logged-in accounts, or the
account new sessions start in — and only then restart the sampler, which re-reads the
gauge for the new login (glideslope.py expires its cache on a login change too).

Pure file reads plus one `launchctl kickstart`. No credential is read or written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import glideslope  # noqa: E402

STATE = glideslope.STORE_DIR / "login-fingerprint.json"
SAMPLER_LABEL = "ai.stage11.glideslope.sampler"


def fingerprint() -> dict:
    logins = glideslope.local_login_emails()
    return {"logins": sorted(logins), "selected": glideslope.selected_login_email(logins=logins)}


def previous() -> dict | None:
    try:
        value = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def remember(value: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(tmp, STATE)


def main() -> int:
    current = fingerprint()
    before = previous()
    if before is not None and {k: before.get(k) for k in current} == current:
        return 0
    remember({**current, "changed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    if before is None:
        return 0  # first run only records; nothing has changed yet
    # -k: a sample already in flight read the old login; restarting it costs a gap, never data.
    result = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{SAMPLER_LABEL}"],
                            capture_output=True, text=True, check=False)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} login change {before.get('selected')} → {current['selected']} "
          f"(logins {', '.join(current['logins']) or '—'}); sampler kicked rc={result.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
