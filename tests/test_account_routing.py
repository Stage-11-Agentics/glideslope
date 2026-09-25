"""Several Claude logins on one satellite: who is here, who is selected, where to go next."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import glideslope
from test_glideslope import NOW, claude_snapshot_with_charlie

LOCAL = glideslope.LOCAL_SATELLITE


def write_login(path: Path, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))


class LocalLoginTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.default = root / ".claude.json"
        self.profiles = root / ".claude-profiles"
        self.selection = root / "selection.json"
        write_login(self.default, "lab@example.com")
        write_login(self.profiles / "bravo" / ".claude.json", "personal@example.com")
        (self.profiles / "empty").mkdir()  # a home nobody has logged into yet

    def tearDown(self):
        self._tmp.cleanup()

    def logins(self):
        return glideslope.local_login_emails(self.default, self.profiles)

    def test_every_home_with_a_login_counts_default_first(self):
        self.assertEqual(self.logins(), ["lab@example.com", "personal@example.com"])

    def test_no_selection_means_the_default_home(self):
        self.assertEqual(glideslope.selected_login_email(
            self.selection, logins=self.logins(), default_config=self.default), "lab@example.com")

    def test_the_selection_names_the_account_new_sessions_use(self):
        self.selection.write_text(json.dumps({"email": "personal@example.com"}))
        self.assertEqual(glideslope.selected_login_email(
            self.selection, logins=self.logins(), default_config=self.default),
            "personal@example.com")

    def test_a_selection_without_a_login_falls_back_like_the_launcher(self):
        self.selection.write_text(json.dumps({"email": "work@example.com"}))
        self.assertEqual(glideslope.selected_login_email(
            self.selection, logins=self.logins(), default_config=self.default), "lab@example.com")


def routed_accounts():
    """Alpha (5h at 100%), Bravo, Charlie (selected) — all logged in on this satellite."""
    accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
    for account in accounts:
        account["logins"] = [LOCAL]
    return accounts


def by_display(accounts, display):
    return next(account for account in accounts if account["display"] == display)


def set_used(account, meter_id, percent):
    next(limit for limit in account["limits"] if limit["meter_id"] == meter_id)["used_percent"] = percent


class SnapshotLoginMatchTests(unittest.TestCase):
    SNAPSHOT = {
        "lab": {"email": "lab@example.com", "active": True, "logged_in": True, "limits": []},
        "personal": {"email": "personal@example.com", "active": False, "logged_in": False, "limits": []},
    }

    def test_unchanged_logins_keep_the_cache(self):
        self.assertTrue(glideslope.snapshot_matches_logins(
            self.SNAPSHOT, ["lab@example.com"], "lab@example.com"))

    def test_a_new_selected_account_expires_the_cache(self):
        self.assertFalse(glideslope.snapshot_matches_logins(
            self.SNAPSHOT, ["personal@example.com"], "personal@example.com"))

    def test_a_new_login_beside_the_selected_one_expires_the_cache(self):
        self.assertFalse(glideslope.snapshot_matches_logins(
            self.SNAPSHOT, ["lab@example.com", "personal@example.com"], "lab@example.com"))

    def test_an_old_snapshot_is_judged_on_the_selected_account_alone(self):
        old = {"personal": {"email": "personal@example.com", "active": True, "limits": []}}
        self.assertTrue(glideslope.snapshot_matches_logins(
            old, ["personal@example.com", "lab@example.com"], "personal@example.com"))

    def test_a_login_change_rereads_before_the_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(self.SNAPSHOT))
            fresh = {"personal": {"email": "personal@example.com", "active": True,
                            "logged_in": True, "limits": []}}
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                return type("R", (), {"returncode": 0, "stdout": json.dumps(fresh), "stderr": ""})()

            from unittest import mock
            with mock.patch.object(glideslope.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(glideslope.shutil, "which", return_value="/bin/claude-account"):
                kept, _, _ = glideslope.load_claude_snapshot(
                    path, logins=(["lab@example.com"], "lab@example.com"))
                self.assertEqual(calls, [])
                self.assertIn("lab", kept)
                switched, _, _ = glideslope.load_claude_snapshot(
                    path, logins=(["personal@example.com"], "personal@example.com"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(list(switched), ["personal"])


class DeadlineTests(unittest.TestCase):
    def test_a_wedged_query_is_abandoned_at_the_deadline(self):
        import threading
        import time
        release = threading.Event()
        started = time.monotonic()
        with self.assertRaisesRegex(glideslope.PositionError, "codex read exceeded"):
            glideslope.call_with_deadline(lambda: release.wait(10), 0.2, "codex")
        self.assertLess(time.monotonic() - started, 2)
        release.set()

    def test_a_query_error_surfaces_on_the_caller(self):
        def boom():
            raise glideslope.PositionError("kimi down")
        with self.assertRaisesRegex(glideslope.PositionError, "kimi down"):
            glideslope.call_with_deadline(boom, 1, "kimi")

    def test_a_prompt_query_returns_its_value(self):
        self.assertEqual(glideslope.call_with_deadline(lambda: {"ok": 1}, 1, "kimi"), {"ok": 1})


class SatelliteLoginTests(unittest.TestCase):
    def test_every_local_login_and_every_beacon_login_is_recorded(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        satellites = [{"name": "studio", "login_email": "personal@example.com",
                       "login_emails": ["personal@example.com", "work@example.com"]}]
        glideslope.reconcile_satellite_logins(
            accounts, ["charlie@example.test", "personal@example.com"], satellites)
        self.assertEqual(by_display(accounts, "Charlie")["logins"], [LOCAL])
        self.assertEqual(by_display(accounts, "Bravo")["logins"], [LOCAL, "studio"])
        self.assertEqual(by_display(accounts, "Alpha")["logins"], ["studio"])

    def test_an_old_beacon_with_one_login_still_counts(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        glideslope.reconcile_satellite_logins(
            accounts, "charlie@example.test",
            [{"name": "studio", "login_email": "personal@example.com"}])
        self.assertEqual(by_display(accounts, "Bravo")["logins"], ["studio"])

    def test_banner_leads_with_the_selected_login(self):
        banner = glideslope.render_login_banner(routed_accounts())
        self.assertTrue(banner.startswith(f"**● Logged in · {LOCAL}: Claude · Charlie**"))
        self.assertIn(f"◦ {LOCAL}: Claude · Bravo", banner)


class PickTests(unittest.TestCase):
    def test_stays_while_every_meter_is_under_the_line(self):
        pick = glideslope.pick_account(routed_accounts(), NOW)
        self.assertEqual((pick["display"], pick["stay"]), ("Charlie", True))
        self.assertEqual(pick["email"], "charlie@example.test")

    def test_moves_when_a_weekly_crosses_the_line(self):
        accounts = routed_accounts()
        set_used(by_display(accounts, "Charlie"), "weekly_fable", 95.0)
        pick = glideslope.pick_account(accounts, NOW)
        # Alpha's 5h session is at 100%, so it is no refuge either — Bravo it is.
        self.assertEqual((pick["display"], pick["stay"]), ("Bravo", False))
        self.assertIn("Charlie Fable weekly at 95%", pick["reason"])

    def test_a_hot_session_counts_as_over_the_line(self):
        accounts = routed_accounts()
        set_used(by_display(accounts, "Charlie"), "session", 91.0)
        self.assertEqual(glideslope.pick_account(accounts, NOW)["display"], "Bravo")

    def test_never_picks_an_account_without_a_login_here(self):
        accounts = routed_accounts()
        set_used(by_display(accounts, "Charlie"), "weekly_all", 99.0)
        by_display(accounts, "Bravo")["logins"] = ["studio"]
        pick = glideslope.pick_account(accounts, NOW)
        self.assertEqual((pick["display"], pick["stay"]), ("Charlie", True))
        self.assertIn("no other login here has room", pick["reason"])

    def test_never_picks_a_dormant_account(self):
        accounts = routed_accounts()
        set_used(by_display(accounts, "Charlie"), "weekly_all", 99.0)
        by_display(accounts, "Bravo")["dormant"] = True
        self.assertEqual(glideslope.pick_account(accounts, NOW)["display"], "Charlie")

    def test_no_login_at_all_is_a_non_answer(self):
        accounts = routed_accounts()
        for account in accounts:
            account["logins"] = []
        self.assertIsNone(glideslope.pick_account(accounts, NOW)["email"])


if __name__ == "__main__":
    unittest.main()
