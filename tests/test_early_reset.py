"""Early resets: a meter cleared mid-window, with its deadline left where it was."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import glideslope
import notify

UTC = dt.timezone.utc
RESET = dt.datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
WEEK = 7 * 24 * 60
START = RESET - dt.timedelta(minutes=WEEK)
CLEARED = dt.datetime(2026, 9, 30, 10, 41, 7, tzinfo=UTC)


def iso(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def store(rows: list[tuple[dt.datetime, str, float, dt.datetime]]) -> Path:
    """A throwaway sample store holding (observed, meter, used, resets_at) rows for Alpha."""
    path = Path(tempfile.mkdtemp(prefix="glideslope-reset-")) / "samples.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE samples (ts TEXT, provider TEXT, account TEXT, display TEXT,"
                " meter TEXT, used_percent REAL, spend_usd REAL, window_minutes INTEGER,"
                " resets_at TEXT, observed_at TEXT, active INTEGER, held_by TEXT)")
    for observed, meter, used, resets in rows:
        con.execute("INSERT INTO samples VALUES (?, 'Claude', 'work', 'Alpha', ?, ?, NULL,"
                    " ?, ?, ?, 1, NULL)", (iso(observed), meter, used, WEEK, iso(resets), iso(observed)))
    con.commit()
    con.close()
    return path


def alpha(used_all: float, used_fable: float, observed: dt.datetime) -> dict:
    return {"provider": "Claude", "account": "work", "display": "Alpha",
            "observed_at": iso(observed), "logins": ["laptop"],
            "limits": [{"meter_id": "weekly_all", "window_minutes": WEEK, "resets_at": RESET,
                        "used_percent": used_all},
                       {"meter_id": "weekly_fable", "window_minutes": WEEK, "resets_at": RESET,
                        "used_percent": used_fable}]}


class DetectTest(unittest.TestCase):
    def test_a_fall_to_the_floor_is_a_reset(self):
        t = [CLEARED - dt.timedelta(minutes=m) for m in (10, 5, 0)]
        events = glideslope.detect_early_resets([(t[0], 99), (t[1], 100), (t[2], 0)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["at"], t[2])
        self.assertEqual(events[0]["from_percent"], 100)

    def test_rounding_and_partial_falls_are_not(self):
        t = [CLEARED - dt.timedelta(minutes=m) for m in (15, 10, 5, 0)]
        self.assertEqual(glideslope.detect_early_resets(
            [(t[0], 100), (t[1], 99), (t[2], 60), (t[3], 58)]), [])


class MarkTest(unittest.TestCase):
    def setUp(self):
        before, after = CLEARED - dt.timedelta(minutes=4), CLEARED
        self.db = store([(before, "weekly_all", 100, RESET), (before, "weekly_fable", 3, RESET),
                         (after, "weekly_all", 0, RESET), (after, "weekly_fable", 0, RESET)])

    def test_the_account_is_rebased_and_siblings_inherit(self):
        now = CLEARED + dt.timedelta(hours=1)
        account = alpha(2, 0, now)
        glideslope.mark_early_resets([account], now, db=self.db)
        weekly, fable = account["limits"]
        self.assertEqual(weekly["rebased_at"], CLEARED)
        # Fable fell 3 points, too little to show a reset; it shares the clock, so it inherits it
        self.assertEqual(fable["rebased_at"], CLEARED)
        self.assertTrue(fable["early_resets"][0]["inherited"])

    def test_the_reading_in_hand_counts_before_it_is_stored(self):
        store_only_before = store([(CLEARED - dt.timedelta(minutes=4), "weekly_all", 100, RESET)])
        account = alpha(0, 0, CLEARED)
        glideslope.mark_early_resets([account], CLEARED, db=store_only_before)
        self.assertEqual(account["limits"][0]["rebased_at"], CLEARED)

    def test_the_previous_window_is_not_this_one(self):
        previous = RESET - dt.timedelta(days=7)
        db = store([(START - dt.timedelta(minutes=1), "weekly_all", 100, previous),
                    (START + dt.timedelta(minutes=5), "weekly_all", 0, RESET)])
        account = alpha(0, 0, START + dt.timedelta(minutes=6))
        glideslope.mark_early_resets([account], START + dt.timedelta(minutes=6), db=db)
        self.assertNotIn("rebased_at", account["limits"][0])

    def test_pace_and_exhaustion_run_from_the_reset(self):
        now = CLEARED + (RESET - CLEARED) / 2
        limit = {"resets_at": RESET, "window_minutes": WEEK, "used_percent": 25.0,
                 "rebased_at": CLEARED}
        self.assertAlmostEqual(glideslope.even_pace_percent(limit, now), 50.0, places=3)
        # 25% in half the budget's span: the rate lands it at 50% by the deadline, not exhausted
        self.assertIsNone(glideslope.exhausts_at(limit, now))
        limit["used_percent"] = 75.0
        self.assertIsNotNone(glideslope.exhausts_at(limit, now))


class NotifyTest(unittest.TestCase):
    def test_a_reset_opens_a_new_threshold_instance(self):
        alert = {"account": "Alpha", "meter_id": "weekly_all", "resets_at": RESET}
        before = notify.window_key(alert)
        after = notify.window_key({**alert, "rebased_at": CLEARED})
        self.assertNotEqual(before, after)
        # the clock is still the last field, so prune keeps it until the deadline
        self.assertIn(after, notify.prune({after: "x"}, CLEARED))

    def test_one_banner_per_account_while_fresh(self):
        now = CLEARED + dt.timedelta(minutes=20)
        account = alpha(1, 0, now)
        for limit in account["limits"]:
            limit["rebased_at"] = CLEARED
            limit["early_resets"] = [{"at": iso(CLEARED), "from_percent": 100, "to_percent": 0,
                                      "inherited": limit["meter_id"] != "weekly_all"}]
        alerts = notify.reset_alerts([account], now)
        self.assertEqual([a["meter_id"] for a in alerts], ["weekly_all"])
        title, body = notify.compose_reset(alerts[0], now)
        self.assertEqual(title, "Alpha · reset used")
        self.assertIn("was 100%", body)
        self.assertIn("× the weekly pace", body)
        key = notify.reset_key(alerts[0])
        self.assertIn(key, notify.prune({key: "x"}, now))
        self.assertEqual(notify.reset_alerts([account], CLEARED + dt.timedelta(hours=3)), [])

    def test_a_jittering_deadline_is_one_window(self):
        alert = {"account": "Alpha", "meter_id": "weekly_all"}
        keys = {notify.window_key({**alert, "resets_at": RESET + dt.timedelta(seconds=d)})
                for d in (-1, 0, 1)}
        self.assertEqual(len(keys), 1)

    def test_old_raw_second_keys_still_match(self):
        path = Path(tempfile.mkdtemp()) / "notified.json"
        path.write_text('{"Alpha|weekly_all|2026-10-04T02:59:59Z": "x"}')
        state = notify.load_state(path)
        self.assertIn(notify.window_key({"account": "Alpha", "meter_id": "weekly_all",
                                         "resets_at": RESET}), state)


if __name__ == "__main__":
    unittest.main()
