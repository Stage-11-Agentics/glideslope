"""A quarantined read stays in samples.db and drops out of every view that reads the store.

Hermetic: a temp store built with the sampler's own schema; the views are built from it.
The rows reproduce 2026-11-01, when Bravo's meters were journaled under Delta.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import quarantine
import sampler

ROOT = Path(__file__).resolve().parents[1]
WEEK = 10_080


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load("glideslope_history_quarantine", ROOT / "views" / "history-src" / "build.py")
NO_DORMANT = Path("/nonexistent/glideslope-test-dormant.json")

DELTA_TRUE = "2026-11-01T18:49:44Z"
DELTA_WRONG = "2026-11-01T18:52:54Z"   # Bravo's meters, written as Delta's
BRAVO = "2026-11-01T18:52:36Z"


def row(at, account, display, meter, used, reset):
    return (at, "Claude", account, display, meter, used, None, WEEK if used is not None else None,
            reset, at, 1, "laptop", None, "Max 20x")


ROWS = [
    row("2026-11-01T18:46:11Z", "delta", "Delta", "weekly_all", 99.0, "2026-11-03T07:59:59Z"),
    row("2026-11-01T18:46:11Z", "delta", "Delta", "weekly_fable", 95.0, "2026-11-03T07:59:59Z"),
    row(DELTA_TRUE, "delta", "Delta", "weekly_all", 99.0, "2026-11-03T08:00:00Z"),
    row(DELTA_TRUE, "delta", "Delta", "weekly_fable", 95.0, "2026-11-03T08:00:00Z"),
    row(BRAVO, "bf", "Bravo", "weekly_all", 83.0, "2026-11-05T09:00:00Z"),
    row(BRAVO, "bf", "Bravo", "weekly_fable", 75.0, "2026-11-05T09:00:00Z"),
    row("2026-11-01T18:53:28Z", "bf", "Bravo", "weekly_all", 83.0, "2026-11-05T09:00:00Z"),
    row("2026-11-01T18:53:28Z", "bf", "Bravo", "weekly_fable", 76.0, "2026-11-05T09:00:00Z"),
    row(DELTA_WRONG, "delta", "Delta", "weekly_all", 83.0, "2026-11-05T09:00:00Z"),
    row(DELTA_WRONG, "delta", "Delta", "weekly_fable", 76.0, "2026-11-05T09:00:00Z"),
    row(DELTA_WRONG, "delta", "Delta", "extra_usage", None, None),
]


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "samples.db"
        con = sqlite3.connect(self.db)
        con.executescript(sampler.SCHEMA)
        sampler.migrate(con)
        con.executemany(
            "INSERT INTO samples (ts, provider, account, display, meter, used_percent, spend_usd,"
            " window_minutes, resets_at, observed_at, active, held_by, credit_minor, plan)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ROWS)
        con.commit()
        con.close()

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = quarantine.main(["--db", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def last_used(self):
        snapshot = history.build_snapshot(self.db, dormant_path=NO_DORMANT)
        return {series["key"]: series["points"][-1][1] for series in snapshot["series"]}

    def count(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        finally:
            con.close()

    def test_a_quarantined_read_leaves_the_views_and_stays_in_the_store(self):
        self.assertEqual(self.last_used()["Delta/weekly_all"], 83.0)
        code, _, _ = self.cli("add", "--provider", "Claude", "--account", "delta",
                              "--observed-at", "2026-11-01T18:52:54+00:00",
                              "--reason", "Bravo's meters, read through a login record that named Delta")
        self.assertEqual(code, 0)
        used = self.last_used()
        self.assertEqual((used["Delta/weekly_all"], used["Delta/weekly_fable"]), (99.0, 95.0))
        self.assertEqual((used["Bravo/weekly_all"], used["Bravo/weekly_fable"]), (83.0, 76.0))
        self.assertEqual(self.count(), len(ROWS))  # append-only: every row is still there
        code, out, _ = self.cli("list")
        self.assertIn(DELTA_WRONG, out)

    def test_a_read_the_store_does_not_hold_is_refused_and_nothing_is_written(self):
        code, _, err = self.cli("add", "--provider", "Claude", "--account", "delta",
                                "--observed-at", "2026-11-01T18:53:00Z", "--reason", "typo")
        self.assertEqual(code, 1)
        self.assertIn("no Claude read", err)
        con = sqlite3.connect(self.db)
        try:
            tables = {name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()
        self.assertNotIn("quarantine", tables)

    def test_a_store_without_a_quarantine_reads_as_it_always_did(self):
        con = quarantine.hide_quarantined(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True))
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM samples").fetchone()[0], len(ROWS))
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
