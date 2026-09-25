"""Coverage for the History view's window reconstruction.

The store holds point-in-time gauges; the history page needs windows. Every test
here is a shape the real store has actually produced — a jittering reset clock, a
Codex week that sags mid-cycle without rolling, a Claude weekly that rolls while
the account is logged out, a run of unanchored zeros after a reset.
"""

import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


BUILDER_PATH = Path(__file__).parents[1] / "views" / "history-src" / "build.py"
SPEC = importlib.util.spec_from_file_location("glideslope_history_builder", BUILDER_PATH)
assert SPEC and SPEC.loader
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)

T0 = int(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
MINUTE = 60_000
HOUR = 60 * MINUTE
WEEK_MINUTES = 10_080
# Hermetic: build_snapshot's default dormant_path is the real
# <store>/dormant.json. Every test pins an explicit path
# that cannot exist, the same discipline these tests already use for the DB
# itself — a test's result must never depend on what this host happens to
# have lying around.
NO_DORMANT = Path("/nonexistent/glideslope-test-dormant.json")


def point(offset_minutes, used, reset_offset_minutes=None, minutes=WEEK_MINUTES):
    """One observation, offsets in minutes from T0."""
    return {
        "t": T0 + offset_minutes * MINUTE,
        "u": float(used),
        "reset": None if reset_offset_minutes is None else T0 + reset_offset_minutes * MINUTE,
        "minutes": minutes,
        "active": True,
    }


class SegmentationTests(unittest.TestCase):
    def test_a_jittering_reset_clock_is_still_one_window(self):
        """The gauge returns a reset time that wanders a second or two between
        reads. Grouping by equality would shred one week into hundreds."""
        points = []
        for index in range(10):
            drift = (index % 3) - 1          # −1s, 0s, +1s, repeating
            points.append({**point(index * 5, index * 3, WEEK_MINUTES),
                           "reset": T0 + WEEK_MINUTES * MINUTE + drift * 1000})
        instances = history.segment(points)
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(instances[0]["points"]), 10)

    def test_a_rolling_window_that_sags_does_not_split(self):
        """Codex's weekly is a rolling window: old usage ages out and the percent
        falls mid-cycle. That is one week, not two."""
        used = [40, 44, 47, 38, 41, 45, 52, 47, 60]
        points = [point(index * 30, value, WEEK_MINUTES) for index, value in enumerate(used)]
        instances = history.segment(points)
        self.assertEqual(len(instances), 1)

    def test_a_roll_the_clock_reported_late_still_splits(self):
        """A fall from something substantial to near nothing is a reset, even if
        the reset clock has not caught up yet."""
        points = [point(0, 60, WEEK_MINUTES), point(5, 94, WEEK_MINUTES),
                  point(10, 0, WEEK_MINUTES), point(15, 2, WEEK_MINUTES)]
        instances = history.segment(points)
        self.assertEqual(len(instances), 2)
        self.assertEqual([p["u"] for p in instances[1]["points"]], [0.0, 2.0])

    def test_a_new_reset_clock_opens_a_new_window(self):
        points = [point(0, 90, WEEK_MINUTES), point(5, 91, WEEK_MINUTES),
                  point(10, 4, 2 * WEEK_MINUTES), point(15, 6, 2 * WEEK_MINUTES)]
        instances = history.segment(points)
        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[1]["reset"], T0 + 2 * WEEK_MINUTES * MINUTE)

    def test_unanchored_zeros_fold_into_the_window_already_running(self):
        """After a reset the clock reads NULL until first use. Those zeros are
        real observations inside the next window's beam, not a window of their own."""
        reset = WEEK_MINUTES                 # the beam therefore opens at T0
        points = [point(5, 0, None), point(10, 0, None),
                  point(60, 3, reset), point(120, 7, reset)]
        instances = history.segment(points)
        self.assertEqual(len(instances), 1)
        self.assertEqual(len(instances[0]["points"]), 4)
        self.assertEqual(instances[0]["reset"], T0 + reset * MINUTE)
        self.assertEqual(instances[0]["points"][0]["t"], T0 + 5 * MINUTE)

    def test_unanchored_zeros_outside_any_beam_stay_their_own_stretch(self):
        """Codex re-anchors on first use, so a long idle run can sit entirely
        before the next window's beam even starts. It has no beam and must not be
        smuggled into one."""
        points = [point(0, 0, None), point(60, 0, None),
                  point(20_000, 1, 20_000 + WEEK_MINUTES)]
        instances = history.segment(points)
        self.assertEqual(len(instances), 2)
        self.assertIsNone(instances[0]["reset"])


class SeriesTests(unittest.TestCase):
    def series(self, points):
        return history.build_series(("Claude", "Alpha", "weekly_all"), points, T0)

    def test_a_closed_window_never_outlives_its_own_reset(self):
        """An account nobody can read sits past its reset with no sample to prove
        the roll. Its span still ends at the reset — the beam is the authority on
        when the window closed, not the last reading."""
        reset = WEEK_MINUTES
        points = [point(0, 10, reset), point(60, 40, reset),
                  # nothing for days, then the next window, already anchored
                  point(WEEK_MINUTES + 3000, 5, 2 * WEEK_MINUTES)]
        built = self.series(points)
        self.assertEqual(len(built["instances"]), 2)
        first = built["instances"][0]
        self.assertEqual(first["span"][1], T0 + reset * MINUTE)
        self.assertFalse(first["open"])
        self.assertTrue(built["instances"][1]["open"])

    def test_the_open_window_keeps_its_reset_ahead_of_now(self):
        points = [point(0, 10, WEEK_MINUTES), point(60, 12, WEEK_MINUTES)]
        built = self.series(points)
        instance = built["instances"][-1]
        self.assertTrue(instance["open"])
        self.assertEqual(instance["span"][1], T0 + WEEK_MINUTES * MINUTE)
        self.assertEqual(instance["beam"], [T0, T0 + WEEK_MINUTES * MINUTE])

    def test_a_beam_runs_one_window_before_the_reset(self):
        points = [point(0, 10, WEEK_MINUTES), point(60, 12, WEEK_MINUTES)]
        beam = self.series(points)["instances"][0]["beam"]
        self.assertEqual(beam[1] - beam[0], WEEK_MINUTES * MINUTE)

    def test_only_gaps_worth_believing_are_reported(self):
        """A missed sample or two costs resolution, not information. A locked-out
        account costs the reading itself, and only that is worth a broken line."""
        points = [point(0, 1, WEEK_MINUTES), point(5, 2, WEEK_MINUTES),
                  point(45, 3, WEEK_MINUTES),           # 40 min — a hiccup
                  point(45 + 4000, 9, WEEK_MINUTES)]    # ~2.8 days — a lockout
        gaps = self.series(points)["gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0], [T0 + 45 * MINUTE, T0 + (45 + 4000) * MINUTE])

    def test_compression_keeps_the_staircase_and_both_lips_of_a_gap(self):
        points = ([point(index * 5, 7, WEEK_MINUTES) for index in range(20)]
                  + [point(5000, 7, WEEK_MINUTES), point(5005, 9, WEEK_MINUTES)])
        built = self.series(points)
        kept = {p[0] for p in built["points"]}
        self.assertIn(T0, kept)                                  # first
        self.assertIn(T0 + 95 * MINUTE, kept)                    # last before the gap
        self.assertIn(T0 + 5000 * MINUTE, kept)                  # first after the gap
        self.assertIn(T0 + 5005 * MINUTE, kept)                  # the step up
        self.assertLess(len(built["points"]), len(points))       # the plateau collapsed
        # every kept value is a real observation, never an average of two
        originals = {(p["t"], p["u"]) for p in points}
        for kept_point in built["points"]:
            self.assertIn((kept_point[0], kept_point[1]), originals)

    def test_a_meter_that_never_moved_is_marked_idle(self):
        points = [point(index * 5, 0, WEEK_MINUTES) for index in range(6)]
        self.assertTrue(self.series(points)["idle"])
        points[-1]["u"] = 1.0
        self.assertFalse(self.series(points)["idle"])


class SnapshotTests(unittest.TestCase):
    def store(self, rows):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE samples (ts TEXT, provider TEXT, account TEXT, display TEXT,"
            " meter TEXT, used_percent REAL, spend_usd REAL, window_minutes INTEGER,"
            " resets_at TEXT, observed_at TEXT, active INTEGER)")
        con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        self.addCleanup(path.unlink)
        return path

    def rows(self, count=8, display="Alpha", account="lab"):
        base = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        reset = (base + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for index in range(count):
            at = (base + dt.timedelta(minutes=5 * index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append((at, "Claude", account, display, "weekly_all", float(index),
                        None, WEEK_MINUTES, reset, at, 1))
        return out

    def test_the_spend_meter_is_not_a_series(self):
        """OpenRouter is dollars against no window — there is no approach to fly."""
        rows = self.rows() + [(
            "2026-08-01T00:00:00Z", "OpenRouter", "openrouter", "OpenRouter", "spend",
            None, 12.5, WEEK_MINUTES, None, "2026-08-01T00:00:00Z", 0)]
        snapshot = history.build_snapshot(self.store(rows), dormant_path=NO_DORMANT)
        self.assertEqual([s["key"] for s in snapshot["series"]], ["Alpha/weekly_all"])

    def test_the_page_never_receives_an_alias_or_an_email(self):
        """Same guard the deck build carries: call-signs only, and a leak fails
        the build rather than the viewer."""
        snapshot = history.build_snapshot(self.store(self.rows()), dormant_path=NO_DORMANT)
        history.snapshot_blob(snapshot)                      # clean snapshot passes
        snapshot["series"][0]["display"] = "work"
        with self.assertRaises(SystemExit):
            history.snapshot_blob(snapshot)
        snapshot["series"][0]["display"] = "someone@example.com"
        with self.assertRaises(SystemExit):
            history.snapshot_blob(snapshot)

    def test_a_snapshot_carries_its_own_extent_and_survives_json(self):
        snapshot = history.build_snapshot(self.store(self.rows()), dormant_path=NO_DORMANT)
        blob = json.loads(history.snapshot_blob(snapshot))
        self.assertEqual(blob["extent"][0], blob["series"][0]["points"][0][0])
        self.assertGreaterEqual(blob["extent"][1], blob["now"])
        self.assertEqual(blob["sample_count"], 8)

    def test_a_store_with_nothing_to_draw_fails_loudly(self):
        with self.assertRaises(SystemExit):
            history.build_snapshot(self.store([]), dormant_path=NO_DORMANT)


class DormantTests(unittest.TestCase):
    """A dormant account's trail stops at the day it went dormant. The
    sidecar is written by deck-src/build.py — this page only reads it, and
    treats an absent or unreadable one as "nobody dormant," never as a
    failure this build can be broken by."""

    def dormant_file(self, mapping):
        handle = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
        json.dump(mapping, handle)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(path.unlink)
        return path

    def rows_for(self, display, count, start):
        reset = (start + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for index in range(count):
            at = (start + dt.timedelta(minutes=5 * index)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append((at, "Claude", "acct", display, "weekly_all", float(index),
                        None, WEEK_MINUTES, reset, at, 1))
        return out

    def store(self, rows):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = Path(handle.name)
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE samples (ts TEXT, provider TEXT, account TEXT, display TEXT,"
            " meter TEXT, used_percent REAL, spend_usd REAL, window_minutes INTEGER,"
            " resets_at TEXT, observed_at TEXT, active INTEGER)")
        con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        self.addCleanup(path.unlink)
        return path

    def test_load_dormant_reads_a_bare_date_as_utc_midnight(self):
        path = self.dormant_file({"Alpha": "2026-08-02"})
        dormant = history.load_dormant(path)
        expected = int(dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc).timestamp() * 1000)
        self.assertEqual(dormant["Alpha"], expected)

    def test_a_missing_sidecar_dormants_nobody(self):
        self.assertEqual(history.load_dormant(NO_DORMANT), {})

    def test_an_unreadable_sidecar_dormants_nobody(self):
        handle = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
        handle.write("not json")
        handle.close()
        path = Path(handle.name)
        self.addCleanup(path.unlink)
        self.assertEqual(history.load_dormant(path), {})

    def test_the_trail_stops_at_dormant_since_and_earlier_history_survives(self):
        start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        # 8 samples, 5 minutes apart, spanning T0 .. T0+35m
        rows = self.rows_for("Alpha", 8, start)
        cutoff = start + dt.timedelta(minutes=20)     # keeps offsets 0,5,10,15
        dormant_path = self.dormant_file({"Alpha": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")})
        snapshot = history.build_snapshot(self.store(rows), dormant_path=dormant_path)
        series = next(s for s in snapshot["series"] if s["key"] == "Alpha/weekly_all")
        kept_offsets = sorted(p[1] for p in series["points"])
        self.assertEqual(kept_offsets, [0.0, 1.0, 2.0, 3.0])
        self.assertLess(series["last"][0], int(cutoff.timestamp() * 1000))

    def test_dormancy_never_touches_another_account(self):
        start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        rows = self.rows_for("Alpha", 8, start) + self.rows_for("Bravo", 8, start)
        cutoff = start + dt.timedelta(minutes=20)
        dormant_path = self.dormant_file({"Alpha": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")})
        snapshot = history.build_snapshot(self.store(rows), dormant_path=dormant_path)
        alpha = next(s for s in snapshot["series"] if s["key"] == "Alpha/weekly_all")
        bravo = next(s for s in snapshot["series"] if s["key"] == "Bravo/weekly_all")
        # Alpha clipped to its first four samples; Bravo, not named in the
        # sidecar, kept every one of its eight.
        self.assertEqual({p[1] for p in alpha["points"]}, {0.0, 1.0, 2.0, 3.0})
        self.assertEqual({p[1] for p in bravo["points"]}, {float(i) for i in range(8)})

    def test_a_dormant_account_with_no_history_before_cutoff_fails_loudly(self):
        """Clipping every sample of the store's only account to nothing is the
        same honest "nothing to draw" as an empty store — never a crash."""
        start = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        rows = self.rows_for("Alpha", 8, start)
        # dormant since before every sample was taken
        dormant_path = self.dormant_file({"Alpha": "2026-07-01"})
        with self.assertRaises(SystemExit):
            history.build_snapshot(self.store(rows), dormant_path=dormant_path)


if __name__ == "__main__":
    unittest.main()
