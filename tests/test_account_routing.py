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
        return glideslope.local_login_ids(self.default, self.profiles)

    def test_every_home_with_a_login_counts_default_first(self):
        self.assertEqual(self.logins(), ["lab@example.com", "personal@example.com"])

    def test_no_selection_means_the_default_home(self):
        self.assertEqual(glideslope.selected_login_id(
            self.selection, logins=self.logins(), default_config=self.default), "lab@example.com")

    def test_the_selection_names_the_account_new_sessions_use(self):
        self.selection.write_text(json.dumps({"email": "personal@example.com"}))
        self.assertEqual(glideslope.selected_login_id(
            self.selection, logins=self.logins(), default_config=self.default),
            "personal@example.com")

    def test_a_selection_without_a_login_falls_back_like_the_launcher(self):
        self.selection.write_text(json.dumps({"email": "work@example.com"}))
        self.assertEqual(glideslope.selected_login_id(
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
    def test_only_the_selected_login_counts_as_logged_in(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        satellites = [{"name": "studio", "login_email": "personal@example.com",
                       "login_emails": ["personal@example.com", "work@example.com"]}]
        glideslope.reconcile_satellite_logins(
            accounts, ["charlie@example.test", "personal@example.com"], satellites,
            live_login="charlie@example.test")
        self.assertEqual(by_display(accounts, "Charlie")["logins"], [LOCAL])
        self.assertEqual(by_display(accounts, "Bravo")["logins"], ["studio"])
        self.assertEqual(by_display(accounts, "Alpha")["logins"], [])

    def test_every_login_home_is_held_even_when_not_selected(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        satellites = [{"name": "studio", "login_email": "personal@example.com",
                       "login_emails": ["personal@example.com", "work@example.com"]}]
        glideslope.reconcile_satellite_logins(
            accounts, ["charlie@example.test", "personal@example.com"], satellites,
            live_login="charlie@example.test")
        self.assertEqual(by_display(accounts, "Charlie")["held_on"], [LOCAL])
        self.assertEqual(by_display(accounts, "Bravo")["held_on"], [LOCAL, "studio"])
        self.assertEqual(by_display(accounts, "Alpha")["held_on"], ["studio"])

    def test_one_machine_is_logged_in_to_one_account(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        glideslope.reconcile_satellite_logins(
            accounts, ["charlie@example.test", "personal@example.com", "work@example.com"], [],
            live_login="work@example.com")
        here = [a["display"] for a in accounts
                if a.get("provider") == "Claude" and LOCAL in a["logins"]]
        self.assertEqual(here, ["Alpha"])

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


class TwoOrganizationsOneEmailTests(unittest.TestCase):
    """One sign-in, two subscriptions: Bravo and Delta share an email, never an identity."""

    SNAPSHOT = {
        "old": {"email": "same@example.com", "org_uuid": "org-old", "active": False, "logged_in": True,
                "limits": [{"label": "session (5h)", "percent": 60.0, "resets_at": "2026-10-30T22:00:00+00:00"}]},
        "new": {"email": "same@example.com", "org_uuid": "org-new", "active": True, "logged_in": True,
                "limits": [{"label": "session (5h)", "percent": 1.0, "resets_at": "2026-10-30T22:00:00+00:00"}]},
    }

    def setUp(self):
        self._signs = dict(glideslope.CLAUDE_CALL_SIGNS)
        glideslope.CLAUDE_CALL_SIGNS.update({"old": "Bravo", "new": "Delta"})
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.default = root / ".claude.json"
        self.profiles = root / ".claude-profiles"
        self.selection = root / "selection.json"
        for path, org in ((self.default, "ORG-NEW"), (self.profiles / "bravo" / ".claude.json", "org-old")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"oauthAccount": {"emailAddress": "same@example.com",
                                                         "organizationUuid": org}}))

    def tearDown(self):
        glideslope.CLAUDE_CALL_SIGNS.clear()
        glideslope.CLAUDE_CALL_SIGNS.update(self._signs)
        self._tmp.cleanup()

    def accounts(self):
        return glideslope.normalize_claude(json.loads(json.dumps(self.SNAPSHOT)), NOW)

    def test_each_home_is_its_own_login(self):
        logins = glideslope.local_login_ids(self.default, self.profiles)
        self.assertEqual(logins, ["same@example.com#org-new", "same@example.com#org-old"])
        self.selection.write_text(json.dumps({"email": "same@example.com", "org_uuid": "org-old"}))
        self.assertEqual(glideslope.selected_login_id(self.selection, logins=logins, default_config=self.default),
                         "same@example.com#org-old")

    def test_an_email_only_selection_names_one_concrete_home(self):
        logins = glideslope.local_login_ids(self.default, self.profiles)
        self.selection.write_text(json.dumps({"email": "same@example.com"}))
        self.assertEqual(glideslope.selected_login_id(self.selection, logins=logins, default_config=self.default),
                         "same@example.com#org-new")

    def test_only_the_live_organization_is_active(self):
        accounts = glideslope.reconcile_login(self.accounts(), "same@example.com#org-old")
        self.assertEqual({a["account"]: a["active"] for a in accounts}, {"old": True, "new": False})

    def test_the_cached_gauge_must_match_both_organizations(self):
        logins = ["same@example.com#org-new", "same@example.com#org-old"]
        self.assertTrue(glideslope.snapshot_matches_logins(self.SNAPSHOT, logins, logins[0]))
        self.assertFalse(glideslope.snapshot_matches_logins(self.SNAPSHOT, logins, logins[1]))

    def test_a_satellite_is_logged_into_one_organization(self):
        accounts = self.accounts()
        glideslope.reconcile_satellite_logins(
            accounts, [], [{"name": "studio", "login_email": "same@example.com#org-old",
                            "login_emails": ["same@example.com#org-old"]}], live_login=None)
        self.assertEqual({a["account"]: a["logins"] for a in accounts}, {"old": ["studio"], "new": []})

    def test_a_satellite_reading_joins_by_organization_and_an_email_alone_is_skipped(self):
        snapshot = json.loads(json.dumps(self.SNAPSHOT))
        snapshot["old"].update(stale=True, fetched_at="2026-10-30T10:00:00+00:00")
        fresh = {"email": "same@example.com", "org_uuid": "org-old", "logged_in": True,
                 "limits": [{"label": "session (5h)", "percent": 75.0, "resets_at": None}]}
        satellite = {"name": "studio", "snapshot": {"bf": fresh}, "observed_at": NOW, "age_seconds": 10}
        glideslope.merge_satellite_claude(snapshot, [satellite], NOW, warnings=[])
        self.assertEqual(snapshot["old"]["limits"][0]["percent"], 75.0)
        self.assertEqual(snapshot["new"]["limits"][0]["percent"], 1.0)

        snapshot = json.loads(json.dumps(self.SNAPSHOT))
        snapshot["old"].update(stale=True)
        bare = {**fresh, "org_uuid": None}
        warnings: list[str] = []
        glideslope.merge_satellite_claude(snapshot, [{**satellite, "snapshot": {"bf": bare}}], NOW,
                                          warnings=warnings)
        self.assertEqual(snapshot["old"]["limits"][0]["percent"], 60.0)
        self.assertTrue(any("share it" in warning for warning in warnings))

    def test_plans_and_dormancy_join_by_alias_not_email(self):
        roster = {"old": {"email": "same@example.com", "tier": "default_claude_max_5x", "dormant": True},
                  "new": {"email": "same@example.com", "tier": "default_claude_max_20x"}}
        accounts = glideslope.reconcile_plans(self.accounts(), roster)
        glideslope.reconcile_dormant(accounts, roster)
        self.assertEqual({a["account"]: (a["plan"], a["dormant"]) for a in accounts},
                         {"old": ("Max", True), "new": ("Ultra", False)})

    def test_an_old_beacon_naming_only_the_shared_email_credits_neither(self):
        accounts = self.accounts()
        glideslope.reconcile_satellite_logins(
            accounts, [], [{"name": "studio", "login_email": "same@example.com",
                            "login_emails": ["same@example.com"]}], live_login=None)
        self.assertEqual({a["account"]: a["held_on"] for a in accounts}, {"old": [], "new": []})

    def test_an_email_only_observer_state_is_not_a_switch(self):
        root = Path(self._tmp.name)
        state, switches = root / "state.json", root / "switches.jsonl"
        state.write_text(json.dumps({"schema": 1, "email": "same@example.com", "alias": "old",
                                     "observed_at": "2026-10-30T10:00:00Z"}))
        event = glideslope.observe_login_switch(
            self.accounts(), "same@example.com#org-new", NOW, state_path=state,
            history_path=root / "history.jsonl", switch_path=switches, lock_path=root / "lock")
        self.assertIsNone(event)
        self.assertFalse(switches.exists())
        self.assertEqual(json.loads(state.read_text())["alias"], "new")
