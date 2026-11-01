#!/usr/bin/env python3
"""Quarantined reads: samples kept in samples.db that the views read around.

samples.db is append-only history: a row is never rewritten or deleted, even one later
proved wrong. A read proved to be another account's (2026-11-01: Bravo's meters
journaled under Delta while a login home's record and its credential disagreed) is
instead named in a `quarantine` table of its own, one row per read: provider, account
and the read's observation time, which together name every meter that read wrote.
Naming a read is the only write; the rows it names stay where they are.

A reader calls `hide_quarantined(con)` on its connection. That shadows `samples` with a
TEMP view of the same name (SQLite searches the temp schema before the main one), so
every unqualified `FROM samples` on that connection reads around the quarantine without
a change to the query. It works on read-only connections, and a store with no
quarantine table is left exactly as it was. The sampler writes on its own connection
and never sees the view.

    python3 quarantine.py list [--db PATH]
    python3 quarantine.py add --provider Claude --account ALIAS --observed-at ISO_UTC --reason TEXT [--db PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine (
  provider       TEXT NOT NULL,
  account        TEXT NOT NULL,   -- store alias, as in samples.account
  observed_at    TEXT NOT NULL,   -- the read's observation time, as in samples.observed_at
  reason         TEXT NOT NULL,
  quarantined_at TEXT NOT NULL,   -- UTC ISO
  UNIQUE (provider, account, observed_at)
);
"""

VIEW = """
CREATE TEMP VIEW IF NOT EXISTS samples AS
  SELECT * FROM main.samples AS s
   WHERE NOT EXISTS (SELECT 1 FROM main.quarantine AS q
                      WHERE q.provider = s.provider AND q.account = s.account
                        AND q.observed_at = s.observed_at)
"""


def hide_quarantined(con: sqlite3.Connection) -> sqlite3.Connection:
    """Make every `FROM samples` on CON skip quarantined reads. Returns CON. Never raises."""
    try:
        if con.execute("SELECT 1 FROM main.sqlite_master"
                       " WHERE type = 'table' AND name = 'quarantine'").fetchone():
            con.execute(VIEW)
    except sqlite3.Error:
        pass  # an unreadable quarantine must never cost a view its store
    return con


def stored_time(value: str) -> str:
    """An ISO time in the store's own spelling, `YYYY-MM-DDTHH:MM:SSZ` in UTC."""
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} names no time zone; give it in UTC with a trailing Z")
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(db: Path, provider: str, account: str, observed_at: str, reason: str) -> list[tuple]:
    """Quarantine one read. Returns the sample rows it covers; refuses a read the store does not hold."""
    if not reason.strip():
        raise ValueError("a quarantine needs a reason")
    observed = stored_time(observed_at)
    con = sqlite3.connect(db, timeout=30)
    try:
        rows = con.execute(
            "SELECT meter, used_percent, resets_at FROM samples"
            " WHERE provider = ? AND account = ? AND observed_at = ? ORDER BY meter",
            (provider, account, observed),
        ).fetchall()
        if not rows:
            raise ValueError(f"no {provider} read for {account} observed at {observed} in {db}")
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO quarantine (provider, account, observed_at, reason, quarantined_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (provider, account, observed, reason.strip(),
             dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        con.commit()
    finally:
        con.close()
    return rows


def listed(db: Path) -> list[dict]:
    """Every quarantined read, oldest first. Empty when the store has none."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quarantine'").fetchone():
            return []
        rows = con.execute("SELECT provider, account, observed_at, reason, quarantined_at"
                           " FROM quarantine ORDER BY observed_at").fetchall()
    finally:
        con.close()
    keys = ("provider", "account", "observed_at", "reason", "quarantined_at")
    return [dict(zip(keys, row)) for row in rows]


def default_db() -> Path:
    import glideslope  # the configured store; imported here so readers pay nothing for it

    return glideslope.STORE_DIR / "samples.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quarantine a sample read proved wrong; the row itself stays.")
    parser.add_argument("--db", type=Path, help="samples.db (default: the configured store)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every quarantined read, as JSON")
    put = commands.add_parser("add", help="quarantine one read")
    put.add_argument("--provider", required=True)
    put.add_argument("--account", required=True, help="store alias, as in samples.account")
    put.add_argument("--observed-at", required=True, help="the read's observed_at, UTC ISO")
    put.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    db = args.db or default_db()
    if not db.exists():
        print(f"no sample store at {db}", file=sys.stderr)
        return 2
    if args.command == "list":
        print(json.dumps(listed(db), indent=2))
        return 0
    try:
        rows = add(db, args.provider, args.account, args.observed_at, args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"quarantined": {"provider": args.provider, "account": args.account,
                                      "observed_at": stored_time(args.observed_at)},
                      "rows": [{"meter": m, "used_percent": u, "resets_at": r} for m, u, r in rows]},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
