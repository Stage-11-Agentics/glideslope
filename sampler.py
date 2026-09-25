#!/usr/bin/env python3
"""Glideslope gauge sampler — the 1-minute position time series.

Turns the point-in-time gauge into history: every run appends the current
cross-provider position to <store>/samples.db. The deck
builds its trails, burn rates, and forward calibration from this store, then
the sampler atomically rebuilds the deck from that exact position.

Token discipline — now trivially satisfied, because nothing anywhere writes one:
- This script NEVER touches credentials. It shells the one sanctioned path,
  `glideslope.py --json`.
- `claude-account` borrows the access token Claude Code already keeps, only while
  that token is still valid, and never refreshes or writes it. A 1-minute cadence
  costs one authenticated usage GET per sample and rotates nothing.
- Codex is read via its app-server protocol (read-only); OpenRouter and Kimi via
  static API keys. Grok refreshes its own Grok Build access token in place when
  expired — the same operation Grok's client already runs.

Failure discipline: gaps are fine — the percent is cumulative, so a missed
sample costs resolution, not information. Every failure logs and exits 0;
launchd simply tries again in a minute. A sampler must never page anyone.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import glideslope  # noqa: E402  — the config (store, keys file), nothing that reads a provider

STORE_DIR = glideslope.STORE_DIR
DB_PATH = STORE_DIR / "samples.db"
KEYS_FILE = glideslope.KEYS_FILE
DECK_BUILDER = ROOT / "views" / "deck-src" / "build.py"
HISTORY_BUILDER = ROOT / "views" / "history-src" / "build.py"


KEY_NAMES = ("OPENROUTER_API_KEY", "KIMI_API_KEY")


def subprocess_env() -> dict[str, str]:
    """launchd carries no shell env — recover the static API keys from the
    sanctioned keys file (gitignored, 0600) rather than baking them into a plist.

    Static keys only. Claude's OAuth blob is never touched here — reading it is
    claude-account's business, and it only ever reads.
    """
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    wanted = [name for name in KEY_NAMES if not env.get(name)]
    if wanted and KEYS_FILE.exists():
        for line in KEYS_FILE.read_text().splitlines():
            for name in wanted:
                if line.startswith(f"{name}="):
                    env[name] = line.split("=", 1)[1].strip()
    return env

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts           TEXT NOT NULL,   -- sampler wall clock, UTC ISO
  provider     TEXT NOT NULL,   -- Claude | Codex | Kimi | Grok | OpenRouter
  account      TEXT NOT NULL,   -- store alias (e.g. work/personal/codex/kimi/grok/openrouter)
  display      TEXT NOT NULL,   -- call-sign (Alpha/Bravo/Charlie/Delta/Codex/Kimi/Grok/OpenRouter)
  meter        TEXT NOT NULL,   -- meter_id (session/weekly_all/weekly_fable/...) or 'spend'
  used_percent REAL,            -- NULL for spend rows
  spend_usd    REAL,            -- OpenRouter only
  window_minutes INTEGER,
  resets_at    TEXT,            -- NULL = window not anchored
  observed_at  TEXT NOT NULL,   -- the gauge's own observation time (the truth clock)
  active       INTEGER NOT NULL DEFAULT 0,
  held_by      TEXT             -- satellites signed into this account at observation
                                -- ("laptop", "studio", "laptop+studio" for example; local first;
                                -- NULL = holders unknown, rows before 2026-08-29)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_samples
  ON samples(provider, account, meter, observed_at);
CREATE INDEX IF NOT EXISTS ix_samples_read
  ON samples(provider, account, meter, ts);
"""


def migrate(con: sqlite3.Connection) -> None:
    """Additive columns for a store created before they existed. Idempotent —
    the ALTER either lands once or reports the column already there."""
    try:
        con.execute("ALTER TABLE samples ADD COLUMN held_by TEXT")
    except sqlite3.OperationalError:
        pass  # already present (or the table is new and the schema carried it)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect() -> dict:
    """One position read through the sanctioned, lock-disciplined pipeline."""
    result = subprocess.run(
        # --max-age-seconds under the 60s cadence: every sample is a fresh Claude
        # read, not a gauge up to CLAUDE_CACHE_SECONDS old (one usage GET per
        # logged-in account per minute).
        [sys.executable, str(ROOT / "glideslope.py"), "--json", "--max-age-seconds", "45"],
        capture_output=True, text=True, timeout=180, env=subprocess_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"glideslope.py failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def rebuild_deck(position: dict) -> str:
    """Rebuild from the sample's exact position without a second provider read."""
    result = subprocess.run(
        [sys.executable, str(DECK_BUILDER), "--position-stdin"],
        input=json.dumps(position),
        capture_output=True,
        text=True,
        timeout=60,
        env=subprocess_env(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        detail = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[redacted-email]", detail)
        raise RuntimeError(detail[:400])
    return result.stdout.strip()


def rebuild_history() -> str:
    """Redraw the history view from the store this run just appended to.

    Reads only samples.db — no provider, no credential, no lock — and takes a
    quarter of a second, so it rides the same minute as the deck. It is
    built AFTER the sample is committed and its failure is not the deck's: a
    history page that could not be redrawn costs the newest few minutes of a
    three-week plot, which is not worth losing a sample or a deck over.
    """
    result = subprocess.run(
        [sys.executable, str(HISTORY_BUILDER)],
        capture_output=True, text=True, timeout=120, env=subprocess_env(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(detail[:400])
    return result.stdout.strip()


def fire_alerts(position: dict) -> list[str]:
    """Deliver any threshold alert this reading has just earned.

    This runs on the position already in hand — no second provider read, no
    second token. It is deliberately last and deliberately swallowed: a banner
    that could not be drawn or delivered must never cost the sample, the deck or
    the history, all of which are already committed by the time it runs.

    Once-per-window dedup lives in notify.py, keyed on the window instance; this
    loop runs every minute (raised from five 2026-09-12) and would otherwise re-warn
    sixty times an hour.
    """
    try:
        import notify

        return notify.run(position.get("accounts", []), dt.datetime.now(dt.timezone.utc))
    except Exception:  # noqa: BLE001 — an alert is the least important thing here
        return []


def rows_from(position: dict, ts: str) -> list[tuple]:
    rows: list[tuple] = []
    for account in position.get("accounts", []):
        observed = account.get("observed_at") or ts
        # The source of the burn, written down with the burn: which satellites
        # were signed into this account when the gauge was read. Over time this
        # column IS the fleet's login topology — who held what, when — and it is
        # what lets a view attribute a stretch of trail to the machine that flew it.
        held_by = "+".join(str(name) for name in account.get("logins") or []) or None
        for limit in account.get("limits", []):
            rows.append((
                ts, account["provider"], account["account"], account["display"],
                limit.get("meter_id") or limit.get("label", "unknown"),
                limit.get("used_percent"), None,
                limit.get("window_minutes"), limit.get("resets_at"),
                observed, 1 if account.get("active") else 0, held_by,
            ))
    router = position.get("openrouter")
    if router:
        rows.append((
            ts, "OpenRouter", "openrouter", router.get("display", "OpenRouter"),
            "spend", None, router.get("weekly_usd"),
            7 * 24 * 60, router.get("limit_reset"),
            router.get("observed_at") or ts, 0, None,
        ))
    return rows


def main() -> int:
    ts = utc_now_iso()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        position = collect()
    except Exception as exc:  # a sampler never pages anyone — log, exit 0, retry in 5m
        print(f"{ts} sample skipped: {exc}", file=sys.stderr)
        return 0

    rows = rows_from(position, ts)
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.executescript(SCHEMA)
        migrate(con)
        # INSERT OR IGNORE + the (provider, account, meter, observed_at) unique key
        # means a cached gauge that hasn't moved writes nothing: the store records
        # observations, not sampler heartbeats.
        cur = con.executemany(
            "INSERT OR IGNORE INTO samples"
            " (ts, provider, account, display, meter, used_percent, spend_usd,"
            "  window_minutes, resets_at, observed_at, active, held_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        fresh = cur.rowcount if cur.rowcount != -1 else 0
    finally:
        con.close()

    try:
        deck_result = rebuild_deck(position)
    except Exception as exc:  # keep the sample; expose that the visible artifact did not advance
        print(
            f"{ts} sampled: {len(rows)} meters, {fresh} new observations; "
            f"deck rebuild failed: {exc}",
            file=sys.stderr,
        )
        return 0

    try:
        history_result = rebuild_history()
    except Exception as exc:
        history_result = f"history rebuild failed: {exc}"

    alerted = fire_alerts(position)

    print(
        f"{ts} sampled: {len(rows)} meters, {fresh} new observations; "
        f"{deck_result or 'deck rebuilt'}; {history_result or 'history rebuilt'}"
        + (f"; notified {', '.join(alerted)}" if alerted else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
