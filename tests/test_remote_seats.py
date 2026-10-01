"""Remote agent seats: who is on a cloud sandbox, and which account pays for it.

Attribution only. The burn is already inside each account's own meters, so these
tests pin the reading (deadlines, dedupe, staleness) and the one rendered line,
never a number added to usage.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glideslope
from test_deck_build import deck_builder
from test_glideslope import NOW

BEACON_PATH = Path(__file__).parents[1] / "tools" / "satellite_beacon.py"
SPEC = importlib.util.spec_from_file_location("glideslope_satellite_beacon", BEACON_PATH)
assert SPEC and SPEC.loader
beacon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(beacon)


def stamp(delta: dt.timedelta = dt.timedelta(0)) -> str:
    return glideslope.iso_utc(NOW + delta)


def seat(agent="grok", account="Grok", ticket="RD-12", role="owner", started=None,
         deadline=None, **extra):
    return {"agent": agent, "model": f"{agent}-model", "effort": "high", "account": account,
            "project": "demo", "ticket": ticket, "role": role, "run": "run-1", "box": "sbx-1",
            "started": started or stamp(-dt.timedelta(minutes=3)), "deadline": deadline, **extra}


def seat_file(seats, *, age=dt.timedelta(minutes=2)):
    return {"generated_at": stamp(-age), "source": "launcher", "seats": seats}


class SeatFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "seats.json"

    def tearDown(self):
        self._tmp.cleanup()

    def gather(self, document=None, satellites=(), warnings=None):
        if document is not None:
            self.path.write_text(json.dumps(document))
        warnings = [] if warnings is None else warnings
        return glideslope.gather_remote_seats(NOW, warnings, list(satellites), path=self.path)

    def test_a_seat_past_its_deadline_is_over(self):
        seats = self.gather(seat_file([
            seat(ticket="A-1", deadline=stamp(dt.timedelta(hours=1))),
            seat(ticket="A-2", deadline=stamp(-dt.timedelta(minutes=1))),
            seat(ticket="A-3", deadline=None),
        ]))
        self.assertEqual([row["ticket"] for row in seats["rows"]], ["A-1", "A-3"])
        self.assertEqual(seats["counts"], {"Grok": 2})

    def test_rows_keep_attribution_and_drop_sandbox_noise(self):
        row = self.gather(seat_file([seat()]))["rows"][0]
        self.assertNotIn("box", row)
        self.assertEqual(row["run"], "run-1")
        self.assertEqual((row["agent"], row["account"], row["ticket"], row["source"]),
                         ("grok", "Grok", "RD-12", glideslope.LOCAL_SATELLITE))

    def test_the_same_seat_from_two_sources_counts_once_and_the_fresher_copy_wins(self):
        started = stamp(-dt.timedelta(minutes=30))
        local = seat_file([seat(started=started, model="old")], age=dt.timedelta(minutes=20))
        remote = seat_file([seat(started=started, model="new"),
                            seat(agent="codex", account="Codex", ticket="RD-13", started=started)],
                           age=dt.timedelta(minutes=1))
        seats = self.gather(local, satellites=[{"name": "studio", "seats": remote}])
        self.assertEqual(len(seats["rows"]), 2)
        grok = next(row for row in seats["rows"] if row["agent"] == "grok")
        self.assertEqual((grok["model"], grok["source"]), ("new", "studio"))
        self.assertEqual(seats["counts"], {"Grok": 1, "Codex": 1})

    def test_two_runs_of_one_ticket_started_together_are_two_seats(self):
        seats = self.gather(seat_file([seat(run="r1"), seat(run="r2")]))
        self.assertEqual(len(seats["rows"]), 2)

    def test_a_zoneless_deadline_is_utc_and_an_epoch_deadline_is_read(self):
        naive = (NOW - dt.timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        epoch = (NOW - dt.timedelta(minutes=1)).timestamp()
        seats = self.gather(seat_file([seat(ticket="N", deadline=naive),
                                       seat(ticket="E", deadline=epoch),
                                       seat(ticket="K", deadline=NOW.timestamp() + 600)]))
        self.assertEqual([row["ticket"] for row in seats["rows"]], ["K"])
        self.assertEqual(seats["rows"][0]["deadline"], stamp(dt.timedelta(minutes=10)))

    def test_an_unreadable_deadline_is_kept_and_warned(self):
        warnings = []
        seats = self.gather(seat_file([seat(deadline="soon")]), warnings=warnings)
        self.assertEqual(len(seats["rows"]), 1)
        self.assertIn("unreadable deadline", warnings[0])

    def test_an_absurd_clock_costs_its_source_never_the_position(self):
        for generated_at in (1e30, "9999-12-31T23:59:59", "garbage"):
            warnings = []
            document = {"generated_at": generated_at, "seats": [seat(deadline=1e30)]}
            seats = glideslope.gather_remote_seats(
                NOW, warnings, [{"name": "studio", "seats": document}], path=None)
            self.assertTrue(all(row["stale"] for row in seats["rows"]), generated_at)

    def test_a_file_from_the_future_is_stale_not_fresh(self):
        warnings = []
        seats = self.gather(seat_file([seat()], age=-dt.timedelta(hours=2)), warnings=warnings)
        self.assertTrue(seats["stale"])
        self.assertEqual(seats["counts"], {})
        self.assertIn("future", warnings[0])

    def test_an_invalid_generated_at_in_a_fresh_file_is_stale_not_fresh(self):
        warnings = []
        seats = self.gather({"generated_at": "invalid", "seats": [seat()]}, warnings=warnings)
        self.assertTrue(seats["stale"])
        self.assertEqual(seats["counts"], {})
        self.assertIsNone(seats["stale_age_seconds"])
        self.assertTrue(any("unreadable generated_at (invalid)" in note for note in warnings))
        self.assertIn("stale, age unknown", glideslope.remote_seats_line(seats))

    def test_a_newer_copy_that_ended_the_seat_wins_over_an_older_live_copy(self):
        started = stamp(-dt.timedelta(hours=1))
        older = seat_file([seat(started=started, deadline=stamp(dt.timedelta(hours=2)))],
                          age=dt.timedelta(minutes=20))
        newer = seat_file([seat(started=started, deadline=stamp(-dt.timedelta(minutes=5)))],
                          age=dt.timedelta(minutes=1))
        seats = self.gather(older, satellites=[{"name": "studio", "seats": newer}])
        self.assertEqual(seats["rows"], [])
        self.assertEqual(seats["counts"], {})
        # Whichever side carries it: the newer word decides.
        seats = self.gather(newer, satellites=[{"name": "studio", "seats": older}])
        self.assertEqual(seats["counts"], {})
        # And a newer copy that extended an ended seat's deadline keeps it running.
        ended = seat_file([seat(started=started, deadline=stamp(-dt.timedelta(minutes=5)))],
                          age=dt.timedelta(minutes=20))
        extended = seat_file([seat(started=started, deadline=stamp(dt.timedelta(hours=3)))],
                             age=dt.timedelta(minutes=1))
        seats = self.gather(ended, satellites=[{"name": "studio", "seats": extended}])
        self.assertEqual(seats["counts"], {"Grok": 1})

    def test_a_pathological_local_file_costs_a_warning(self):
        self.path.write_text("[" * 200_000)
        warnings = []
        self.assertIsNone(self.gather(warnings=warnings))
        self.assertIn("unreadable", warnings[0])

    def test_a_different_role_on_the_same_ticket_is_a_different_seat(self):
        seats = self.gather(seat_file([seat(role="owner"), seat(role="reviewer")]))
        self.assertEqual(len(seats["rows"]), 2)

    def test_rows_with_nothing_to_match_on_are_never_merged(self):
        bare = {**seat(ticket=None, role=None), "started": None}
        seats = self.gather(seat_file([bare] * 3))
        self.assertEqual(seats["counts"], {"Grok": 3})

    def test_an_old_file_is_stale_and_never_counted_as_current(self):
        seats = self.gather(seat_file([seat()], age=dt.timedelta(hours=2)))
        self.assertTrue(seats["stale"])
        self.assertEqual(seats["counts"], {})
        self.assertEqual(seats["stale_counts"], {"Grok": 1})
        self.assertTrue(seats["rows"][0]["stale"])
        line = glideslope.remote_seats_line(seats)
        self.assertIn("stale, as of 2h 0m ago", line)
        self.assertEqual(line, "**On remote seats (stale, as of 2h 0m ago):** 1 Grok (Grok)")

    def test_a_file_a_day_old_is_dropped_with_a_warning(self):
        warnings = []
        seats = self.gather(seat_file([seat()], age=dt.timedelta(days=2)), warnings=warnings)
        self.assertEqual(seats["rows"], [])
        self.assertIn("ignored", warnings[0])
        self.assertEqual(glideslope.remote_seats_line(seats), "")

    def test_a_missing_generated_at_falls_back_to_the_files_own_clock(self):
        self.path.write_text(json.dumps({"seats": [seat()]}))
        old = (NOW - dt.timedelta(hours=3)).timestamp()
        os.utime(self.path, (old, old))
        seats = self.gather()
        self.assertTrue(seats["stale"])

    def test_an_absent_or_broken_file_warns_and_reads_nothing(self):
        warnings = []
        self.assertIsNone(self.gather(warnings=warnings))
        self.assertIn("not found", warnings[0])
        self.path.write_text("{not json")
        warnings = []
        self.assertIsNone(self.gather(warnings=warnings))
        self.assertIn("unreadable", warnings[0])

    def test_malformed_rows_are_skipped_and_counted(self):
        warnings = []
        seats = self.gather(seat_file([seat(), {"agent": "codex"}, "nope"]), warnings=warnings)
        self.assertEqual(len(seats["rows"]), 1)
        self.assertIn("2 malformed row(s) skipped", warnings[0])

    def test_no_source_configured_is_no_seats(self):
        self.assertIsNone(glideslope.gather_remote_seats(NOW, [], [], path=None))

    def test_an_email_never_renders_and_an_alias_takes_its_call_sign(self):
        seats = self.gather(seat_file([seat(agent="claude", account="work", ticket="X-1"),
                                       seat(agent="claude", account="who@example.com", ticket="X-2")]))
        line = glideslope.remote_seats_line(seats)
        self.assertIn("Claude (Alpha)", line)
        self.assertNotIn("@", line)


class SeatLineTests(unittest.TestCase):
    def seats(self, *documents):
        return glideslope.gather_remote_seats(
            NOW, [], [{"name": f"sat{i}", "seats": d} for i, d in enumerate(documents)], path=None)

    def fleet(self):
        rows = ([seat(ticket=f"G-{i}") for i in range(6)]
                + [seat(agent="codex", account="Codex", ticket=f"C-{i}") for i in range(2)]
                + [seat(agent="claude", account="Alpha", ticket="A-1")])
        return self.seats(seat_file(rows))

    def test_one_line_grouped_by_agent_with_the_billed_account(self):
        self.assertEqual(glideslope.remote_seats_line(self.fleet()),
                         "**On remote seats:** 6 Grok (Grok) · 2 Codex (Codex) · 1 Claude (Alpha)")

    def test_a_stale_source_beside_a_fresh_one_is_named_stale(self):
        seats = self.seats(seat_file([seat()]),
                           seat_file([seat(agent="claude", account="Bravo", ticket="B-1")],
                                     age=dt.timedelta(hours=3)))
        self.assertEqual(glideslope.remote_seats_line(seats, plain=True),
                         "On remote seats: 1 Grok (Grok) · stale, as of 3h 0m ago: 1 Claude (Bravo)")

    def test_the_report_carries_the_line_after_the_banner_and_nothing_without_seats(self):
        report = glideslope.render_report([], None, None, [], NOW, [], color=False,
                                          remote_seats=self.fleet())
        self.assertIn("**On remote seats:** 6 Grok (Grok)", report)
        self.assertLess(report.index("Logged in"), report.index("On remote seats"))
        bare = glideslope.render_report([], None, None, [], NOW, [], color=False)
        self.assertNotIn("remote seats", bare)
        empty = glideslope.render_report([], None, None, [], NOW, [], color=False,
                                         remote_seats=self.seats(seat_file([])))
        self.assertNotIn("remote seats", empty)

    def test_seats_are_never_an_account_row(self):
        report = glideslope.render_report([], None, None, [], NOW, [], color=False,
                                          remote_seats=self.fleet())
        table = report[report.index("All windows"):]
        self.assertNotIn("Grok (Grok)", table)

    def test_json_shape(self):
        payload = glideslope.snapshot_payload([], None, None, [], NOW, [], [],
                                              remote_seats=self.fleet())
        seats = json.loads(json.dumps(payload))["remote_seats"]
        self.assertEqual(seats["counts"], {"Grok": 6, "Codex": 2, "Alpha": 1})
        self.assertEqual(seats["stale_counts"], {})
        self.assertFalse(seats["stale"])
        self.assertEqual(len(seats["rows"]), 9)
        self.assertEqual(seats["line"],
                         "On remote seats: 6 Grok (Grok) · 2 Codex (Codex) · 1 Claude (Alpha)")
        self.assertEqual(set(seats["rows"][0]), set(glideslope.SEAT_FIELDS) | {"source", "stale", "partial"})
        self.assertFalse(seats["incomplete"])
        self.assertEqual(seats["sources"][0]["generated_at"], stamp(-dt.timedelta(minutes=2)))
        self.assertIsNone(glideslope.snapshot_payload([], None, None, [], NOW, [], [])["remote_seats"])

    def test_the_detail_view_gets_the_plain_line_only(self):
        payload = glideslope.snapshot_payload([], None, None, [], NOW, [], [],
                                              remote_seats=self.fleet())
        snapshot = deck_builder.build_snapshot(json.loads(json.dumps(payload)))
        self.assertEqual(snapshot["remote_seats"], payload["remote_seats"]["line"])
        self.assertNotIn("G-0", json.dumps(snapshot))  # rows stay in --json

    def test_a_satellites_beacon_seats_reach_the_reader(self):
        document = seat_file([seat()])
        reply = {"observed_at": stamp(), "login_email": None, "snapshot": {}, "seats": document}
        with mock.patch.object(glideslope, "query_satellite", return_value=reply), \
             mock.patch.object(glideslope, "cached_provider_read", return_value=None), \
             mock.patch.object(glideslope, "cache_provider_read"):
            satellites = glideslope.read_satellites(
                NOW, [], satellites=({"name": "studio", "host": "studio"},))
        seats = glideslope.gather_remote_seats(NOW, [], satellites, path=None)
        self.assertEqual(seats["rows"][0]["source"], "studio")


class BeaconSeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = root / "config.toml"
        self.seats = root / "seats.json"
        self.out = root / "store" / "satellite.json"
        self.patches = [
            mock.patch.object(beacon, "CONFIG", self.config),
            mock.patch.object(beacon, "BEACON", self.out),
            mock.patch.object(beacon, "gauge", return_value=({}, None, [])),
            mock.patch.object(beacon, "login_emails", return_value=[]),
            mock.patch.object(beacon, "selected_email", return_value=None),
            mock.patch.object(beacon, "held_logins", return_value={}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self._tmp.cleanup()

    def publish(self) -> dict:
        with mock.patch("sys.stdout"):
            self.assertEqual(beacon.main(), 0)
        return json.loads(self.out.read_text())

    def configure(self):
        self.config.write_text(f'[satellite]\nname = "studio"\n\n[seats]\nfile = "{self.seats}"\n')

    def test_the_live_rows_and_the_files_clock_ride_the_beacon(self):
        self.configure()
        now = dt.datetime.now(dt.timezone.utc)
        later = glideslope.iso_utc(now + dt.timedelta(hours=1))
        earlier = glideslope.iso_utc(now - dt.timedelta(minutes=1))
        self.seats.write_text(json.dumps({"generated_at": "2026-10-01T10:00:00Z", "seats": [
            seat(ticket="L-1", deadline=later), seat(ticket="L-2", deadline=earlier),
            seat(ticket="L-3", deadline=None)]}))
        published = self.publish()["seats"]
        self.assertEqual(published["generated_at"], "2026-10-01T10:00:00Z")
        # Live rows first; the one that just ended rides after them, for the reader's dedupe.
        self.assertEqual([row["ticket"] for row in published["seats"]], ["L-1", "L-3", "L-2"])
        self.assertEqual(published["live_total"], 2)
        self.assertNotIn("truncated", published)
        self.assertNotIn("box", published["seats"][0])
        self.assertEqual(published["seats"][0]["run"], "run-1")
        self.assertNotIn("deadline", published["seats"][1])  # null crosses as absent

    def test_a_row_that_ended_long_ago_is_not_carried(self):
        self.configure()
        gone = glideslope.iso_utc(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
        self.seats.write_text(json.dumps({"generated_at": "2026-10-01T10:00:00Z",
                                          "seats": [seat(deadline=gone)]}))
        self.assertEqual(self.publish()["seats"]["seats"], [])

    def test_the_beacon_bounds_what_it_carries(self):
        self.configure()
        long = "x" * 500
        self.seats.write_text(json.dumps({"generated_at": "2026-10-01T10:00:00Z",
                                          "seats": [seat(project=long, ticket=f"T-{i}")
                                                    for i in range(beacon.SEATS_MAX_ROWS + 50)]}))
        carried = self.publish()["seats"]
        self.assertEqual(len(carried["seats"]), beacon.SEATS_MAX_ROWS)
        self.assertEqual(len(carried["seats"][0]["project"]), beacon.SEAT_TEXT_MAX)
        self.assertTrue(carried["truncated"])
        self.assertEqual(carried["live_total"], beacon.SEATS_MAX_ROWS + 50)
        # Through the reader to the rendered line: a capped count is a floor, never exact.
        warnings = []
        seats = glideslope.gather_remote_seats(
            dt.datetime.now(dt.timezone.utc), warnings,
            [{"name": "studio", "seats": carried}], path=None)
        self.assertTrue(seats["incomplete"])
        self.assertTrue(any("lower bounds" in note for note in warnings))
        self.assertIn(f"{beacon.SEATS_MAX_ROWS}+ Grok (Grok)",
                      glideslope.remote_seats_line(seats, plain=True))

    def test_a_capped_fresh_count_renders_as_a_floor(self):
        self.configure()
        now = dt.datetime.now(dt.timezone.utc)
        self.seats.write_text(json.dumps({"generated_at": glideslope.iso_utc(now), "seats": [
            seat(ticket=f"T-{i}") for i in range(beacon.SEATS_MAX_ROWS + 1)]}))
        carried = self.publish()["seats"]
        seats = glideslope.gather_remote_seats(now, [], [{"name": "studio", "seats": carried}],
                                               path=None)
        self.assertEqual(glideslope.remote_seats_line(seats, plain=True),
                         f"On remote seats: {beacon.SEATS_MAX_ROWS}+ Grok (Grok)")
        payload = glideslope.snapshot_payload([], None, None, [], now, [], [], remote_seats=seats)
        self.assertTrue(payload["remote_seats"]["incomplete"])

    def test_an_invalid_generated_at_is_carried_and_read_as_stale(self):
        self.configure()
        now = dt.datetime.now(dt.timezone.utc)
        self.seats.write_text(json.dumps({"generated_at": "invalid", "seats": [seat()]}))
        carried = self.publish()["seats"]
        self.assertEqual(carried["generated_at"], "invalid")  # never swapped for the fresh mtime
        warnings = []
        seats = glideslope.gather_remote_seats(now, warnings, [{"name": "studio", "seats": carried}],
                                               path=None)
        self.assertTrue(seats["stale"])
        self.assertEqual(seats["counts"], {})
        self.assertTrue(any("unreadable generated_at" in note for note in warnings))

    def test_a_beacon_carried_ending_beats_an_older_copy(self):
        """The launcher's satellite says the seat ended; this machine's older file says it runs."""
        self.configure()
        now = dt.datetime.now(dt.timezone.utc)
        started = glideslope.iso_utc(now - dt.timedelta(hours=1))
        self.seats.write_text(json.dumps({"generated_at": glideslope.iso_utc(now), "seats": [
            seat(started=started, deadline=glideslope.iso_utc(now - dt.timedelta(minutes=5)))]}))
        carried = self.publish()["seats"]
        local = Path(self._tmp.name) / "local-seats.json"
        local.write_text(json.dumps({
            "generated_at": glideslope.iso_utc(now - dt.timedelta(minutes=20)),
            "seats": [seat(started=started, deadline=glideslope.iso_utc(now + dt.timedelta(hours=2)))]}))
        seats = glideslope.gather_remote_seats(now, [], [{"name": "studio", "seats": carried}],
                                               path=local)
        self.assertEqual(seats["rows"], [])
        self.assertEqual(glideslope.remote_seats_line(seats), "")

    def test_a_pathological_seat_file_never_stops_the_beacon(self):
        self.configure()
        self.seats.write_text("[" * 200_000)  # deep enough to exhaust the JSON decoder's recursion
        payload = self.publish()
        self.assertTrue(self.out.exists())
        self.assertNotIn("seats", payload)
        self.assertTrue(any("remote seats" in note for note in payload["warnings"]))

    def test_any_failure_reading_seats_still_publishes(self):
        self.configure()
        with mock.patch.object(beacon, "seat_document", side_effect=RuntimeError("boom")):
            payload = self.publish()
        self.assertNotIn("seats", payload)
        self.assertIn("remote seats could not be read: RuntimeError: boom", payload["warnings"])

    def test_no_seats_key_without_the_config(self):
        self.assertNotIn("seats", self.publish())

    def test_an_absent_file_is_omitted_and_warned_never_fatal(self):
        self.configure()
        payload = self.publish()
        self.assertNotIn("seats", payload)
        self.assertTrue(any("not found" in note for note in payload["warnings"]))

    def test_an_unreadable_file_is_omitted_and_warned(self):
        self.configure()
        self.seats.write_text("[1, 2")
        payload = self.publish()
        self.assertNotIn("seats", payload)
        self.assertTrue(any("unreadable" in note for note in payload["warnings"]))

    def test_the_key_is_read_without_tomllib(self):
        self.configure()
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(beacon.seats_path(self.config), self.seats)
        self.config.write_text('[seatsx]\nfile = "/elsewhere"\n')
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertIsNone(beacon.seats_path(self.config))


if __name__ == "__main__":
    unittest.main()
