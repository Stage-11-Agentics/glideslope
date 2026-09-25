import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import glideslope
import notify


NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
HERE = glideslope.LOCAL_SATELLITE


def account(display="Charlie", *, logins=None, stale=False, session=None,
            weekly=None, fable=None, alias="lab"):
    def limit(meter, label, percent, minutes, hours_left, presumed=False):
        return {"meter_id": meter, "label": label, "used_percent": percent,
                "window_minutes": minutes, "presumed": presumed,
                "resets_at": NOW + dt.timedelta(hours=hours_left), "anchored": True}

    limits = []
    if session is not None:
        limits.append(limit("session", "5h session", *session))
    if weekly is not None:
        limits.append(limit("weekly_all", "Weekly · all models", *weekly))
    if fable is not None:
        limits.append(limit("weekly_fable", "Weekly · Fable", *fable))
    return {"provider": "Claude", "display": display, "account": alias, "stale": stale,
            "active": HERE in (logins or []), "logins": logins if logins is not None else [HERE],
            "limits": limits}


class SelectionTests(unittest.TestCase):
    """What is worth interrupting someone for, and what is emphatically not."""

    def test_a_window_over_the_line_is_selected(self):
        alerts = notifiable(account(weekly=(92.0, 10080, 30)))
        self.assertEqual([a["meter_id"] for a in alerts], ["weekly_all"])
        self.assertEqual(alerts[0]["account"], "Charlie")

    def test_a_window_under_the_line_is_not(self):
        self.assertEqual(notifiable(account(weekly=(89.9, 10080, 30))), [])

    def test_all_three_windows_can_cross(self):
        alerts = notifiable(account(session=(95.0, 300, 1), weekly=(91.0, 10080, 30),
                                    fable=(99.0, 10080, 30)))
        self.assertEqual({a["meter_id"] for a in alerts},
                         {"session", "weekly_all", "weekly_fable"})

    def test_an_account_logged_in_only_elsewhere_is_never_alerted(self):
        """studio crossing 90% is news nobody at this keyboard can act on, and the
        satellite holding it has no one looking at its screen."""
        self.assertEqual(notifiable(account(logins=["studio"], weekly=(97.0, 10080, 30))), [])

    def test_an_account_logged_in_nowhere_is_never_alerted(self):
        self.assertEqual(notifiable(account(logins=[], weekly=(97.0, 10080, 30))), [])

    def test_a_presumed_window_cannot_cross_a_threshold(self):
        one = account(weekly=(0.0, 10080, 30))
        one["limits"][0].update(used_percent=0.0, presumed=True)
        self.assertEqual(notifiable(one), [])

    def test_a_stale_read_still_counts_because_a_floor_at_92_is_at_least_92(self):
        alerts = notifiable(account(stale=True, weekly=(92.0, 10080, 30)))
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["floor"])

    def test_a_window_whose_reset_already_passed_is_not_alerted(self):
        self.assertEqual(notifiable(account(weekly=(95.0, 10080, -1))), [])

    def test_the_alert_carries_the_store_alias_the_sample_db_is_keyed_by(self):
        self.assertEqual(notifiable(account(weekly=(92.0, 10080, 30)))[0]["alias"], "lab")


def notifiable(*accounts):
    return glideslope.notifiable_windows(list(accounts), NOW)


class AlternativeTests(unittest.TestCase):
    """Where to go instead — ranked by room against the clock, not raw percent."""

    def test_room_is_measured_against_the_clock_not_by_raw_percent(self):
        """Two accounts at exactly 40%, and they are not equivalent at all.

        Alpha is six days into its week — 60 points that expire in twelve hours,
        which is capacity already paid for and about to be lost. Bravo is one day
        in at 40%, burning well above its own beam and heading for the wall. The
        offer is the one with capacity to lose, which is the whole 'below the beam
        is also a defect' half of this instrument's thesis.
        """
        early = account("Bravo", alias="personal", logins=[], weekly=(40.0, 10080, 144))
        late = account("Alpha", alias="work", logins=[], weekly=(40.0, 10080, 12))
        best = glideslope.best_alternative([early, late], NOW, exclude="Charlie")
        self.assertEqual(best["display"], "Alpha")
        self.assertEqual(best["meter_id"], "weekly_all")  # the only weekly this fixture sets
        self.assertGreater(best["slack"], 0)

    def test_a_lower_percent_can_still_lose_to_a_higher_one(self):
        """20% one day in is behind budget; 60% six days in is money on the table."""
        fresh = account("Bravo", alias="personal", logins=[], weekly=(20.0, 10080, 144))
        stretched = account("Alpha", alias="work", logins=[], weekly=(60.0, 10080, 12))
        best = glideslope.best_alternative([fresh, stretched], NOW, exclude="Charlie")
        self.assertEqual(best["display"], "Alpha")

    def test_an_account_over_the_line_itself_is_no_refuge(self):
        self.assertIsNone(glideslope.best_alternative(
            [account("Bravo", alias="personal", logins=[], weekly=(96.0, 10080, 144))],
            NOW, exclude="Charlie"))

    def test_the_account_being_warned_about_is_never_offered_as_its_own_escape(self):
        self.assertIsNone(glideslope.best_alternative(
            [account("Charlie", weekly=(20.0, 10080, 144))], NOW, exclude="Charlie"))

    def test_it_reads_the_json_shape_too(self):
        """The sampler holds the --json payload, where resets_at is an ISO string.
        Returning None there would silently stop offering anywhere to swap."""
        raw = account("Bravo", alias="personal", logins=[], weekly=(20.0, 10080, 144))
        raw["limits"][0]["resets_at"] = glideslope.iso_utc(raw["limits"][0]["resets_at"])
        self.assertEqual(glideslope.best_alternative([raw], NOW, exclude="Charlie")["display"],
                         "Bravo")


class ComposeTests(unittest.TestCase):
    def test_the_body_says_when_it_resets_when_it_dies_and_where_to_go(self):
        alert = notifiable(account(weekly=(92.0, 10080, 30)))[0]
        other = {"display": "Bravo", "used_percent": 18.0, "pace_percent": 33.0, "slack": 15.0,
                 "floor": False}
        title, body = notify.compose(alert, other, NOW)
        self.assertIn("Charlie", title)
        self.assertIn("92%", title)
        self.assertIn("Resets in", body)
        self.assertIn("gone by", body)          # the projection
        self.assertIn("Bravo has the most room", body)   # the action

    def test_a_floor_says_so_in_the_title(self):
        alert = notifiable(account(stale=True, weekly=(92.0, 10080, 30)))[0]
        self.assertIn("92%+", notify.compose(alert, None, NOW)[0])

    def test_a_multi_day_reset_wears_its_day(self):
        """'gone by 1:33 AM' is a riddle six days out, which is the range a
        seven-day window covers."""
        alert = notifiable(account(weekly=(92.0, 10080, 100)))[0]
        self.assertRegex(notify.compose(alert, None, NOW)[1], r"\([A-Z][a-z]{2} \d")

    def test_a_fable_bound_alternative_says_so(self):
        """best_alternative names which weekly bound it; the message should say
        so when it's the less obvious of the two."""
        alert = notifiable(account(weekly=(92.0, 10080, 30)))[0]
        other = {"display": "Bravo", "used_percent": 18.0, "pace_percent": 33.0, "slack": 15.0,
                 "floor": False, "meter_id": "weekly_fable"}
        body = notify.compose(alert, other, NOW)[1]
        self.assertIn("Bravo has the most room on Fable", body)

    def test_an_all_models_bound_alternative_says_nothing_extra(self):
        alert = notifiable(account(weekly=(92.0, 10080, 30)))[0]
        other = {"display": "Bravo", "used_percent": 18.0, "pace_percent": 33.0, "slack": 15.0,
                 "floor": False, "meter_id": "weekly_all"}
        body = notify.compose(alert, other, NOW)[1]
        self.assertIn("Bravo has the most room —", body)
        self.assertNotIn("on Fable", body)


class DedupTests(unittest.TestCase):
    """The failure this file exists for: the sampler runs every five minutes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "notified.json"
        self.sent = []
        self.ok = True
        self.original = notify.deliver
        notify.deliver = lambda title, body, image=None, **kw: (
            self.sent.append(title) or self.ok) and self.ok
        # a picture needs a sample store and a window; neither belongs in this test
        self.original_draw = notify.draw_approach
        notify.draw_approach = lambda alert, now, size=512: None

    def tearDown(self):
        notify.deliver = self.original
        notify.draw_approach = self.original_draw
        self.dir.cleanup()

    def run_once(self, accounts, now=NOW):
        return notify.run(accounts, now, state_path=self.state)

    def test_crossing_the_line_notifies_exactly_once(self):
        accounts = [account(weekly=(92.0, 10080, 30))]
        self.assertEqual(len(self.run_once(accounts)), 1)
        for _ in range(11):  # an hour of five-minute ticks
            self.assertEqual(self.run_once(accounts), [])
        self.assertEqual(len(self.sent), 1)

    def test_the_next_window_is_a_fresh_chance_to_warn(self):
        """Dedup is keyed on the window INSTANCE — its own reset instant — so a
        new window may warn again on its own merits."""
        self.run_once([account(weekly=(92.0, 10080, 30))])
        later = account(weekly=(93.0, 10080, 30))
        later["limits"][0]["resets_at"] = NOW + dt.timedelta(hours=200)  # the next window
        self.assertEqual(len(self.run_once([later])), 1)
        self.assertEqual(len(self.sent), 2)

    def test_an_undelivered_alert_is_not_recorded_as_told(self):
        """The notify sink being down must postpone the warning, not cancel it."""
        self.ok = False
        accounts = [account(weekly=(92.0, 10080, 30))]
        self.assertEqual(self.run_once(accounts), [])
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.ok = True
        self.assertEqual(len(self.run_once(accounts)), 1)

    def test_state_forgets_windows_that_have_already_reset(self):
        self.run_once([account(weekly=(92.0, 10080, 1))])
        self.assertEqual(len(json.loads(self.state.read_text())), 1)
        self.run_once([], now=NOW + dt.timedelta(hours=2))  # that window is gone
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_two_windows_on_one_account_are_two_alerts(self):
        self.assertEqual(len(self.run_once([account(session=(95.0, 300, 1),
                                                    weekly=(92.0, 10080, 30))])), 2)


class ApproachImageTests(unittest.TestCase):
    def test_it_draws_a_png_for_a_real_window(self):
        alert = notifiable(account(weekly=(92.0, 10080, 30)))[0]
        with tempfile.TemporaryDirectory() as directory:
            original = notify.IMAGE_DIR
            notify.IMAGE_DIR = Path(directory)
            try:
                path = notify.draw_approach(alert, NOW, size=256)
            finally:
                notify.IMAGE_DIR = original
            self.assertIsNotNone(path)
            self.assertTrue(path.exists() and path.stat().st_size > 0)
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_window_with_no_duration_draws_nothing_rather_than_a_lie(self):
        alert = dict(notifiable(account(weekly=(92.0, 10080, 30)))[0], window_minutes=None)
        self.assertIsNone(notify.draw_approach(alert, NOW))


class ExhaustTests(unittest.TestCase):
    """The same arithmetic the plot's flight path draws, kept in one place."""

    def limit(self, percent, hours_left, minutes=10080):
        return {"used_percent": percent, "window_minutes": minutes,
                "resets_at": NOW + dt.timedelta(hours=hours_left)}

    def test_a_rate_that_reaches_the_ceiling_first_returns_when(self):
        # half the window elapsed, 60% burned → 100% at 5/6 of the window
        at = glideslope.exhausts_at(self.limit(60.0, 84), NOW)
        self.assertIsNotNone(at)
        self.assertLess(at, NOW + dt.timedelta(hours=84))

    def test_a_rate_that_does_not_reach_it_returns_none_not_a_far_off_date(self):
        self.assertIsNone(glideslope.exhausts_at(self.limit(10.0, 84), NOW))

    def test_an_unstarted_window_has_no_rate_to_project(self):
        self.assertIsNone(glideslope.exhausts_at(self.limit(0.0, 84), NOW))


if __name__ == "__main__":
    unittest.main()


class SinkTests(unittest.TestCase):
    """The configured delivery sink, with nothing real behind it."""

    def test_the_macos_sink_posts_through_osascript(self):
        from unittest import mock
        with mock.patch.object(notify.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(notify.deliver("Title", "Body 'quoted'", sink="macos"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertIn("display notification", argv[2])
        self.assertIn("Title", argv[2])

    def test_the_macos_sink_reports_failure_without_raising(self):
        from unittest import mock
        with mock.patch.object(notify.subprocess, "run", side_effect=OSError("no osascript")):
            self.assertFalse(notify.deliver("Title", "Body", sink="macos"))

    def test_the_none_sink_and_an_unconfigured_url_sink_deliver_nothing(self):
        self.assertFalse(notify.deliver("Title", "Body", sink="none"))
        self.assertFalse(notify.deliver("Title", "Body", sink="url", url=""))
