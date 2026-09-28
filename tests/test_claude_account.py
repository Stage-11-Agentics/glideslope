"""claude-account's login homes: hermetic — a temp HOME, no keychain, no network."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parent.parent / "tools" / "claude-account"
# The call-sign map every test resolves against: fixture identities, never real ones.
CALL_SIGNS = {
    "work@example.com": "Alpha",
    "personal@example.com": "Bravo",
    "lab@example.com": "Charlie",
    "team@example.com": "Delta",
}


def load_tool(home: Path):
    """Import tools/claude-account with every path rooted in a temp home."""
    loader = importlib.machinery.SourceFileLoader(f"claude_account_{id(home)}", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    with mock.patch("pathlib.Path.home", return_value=home):
        loader.exec_module(module)
    module.CALL_SIGNS = dict(CALL_SIGNS)
    return module


def write_config(path: Path, email: str | None, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(extra)
    if email:
        value["oauthAccount"] = {"emailAddress": email}
    path.write_text(json.dumps(value))


class ToolCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text("{}")
        (self.home / ".claude" / "daemon").mkdir()
        write_config(self.home / ".claude.json", "personal@example.com",
                     hasCompletedOnboarding=True, projects={"/x": {}}, userID="secret-ish")
        self.tool = load_tool(self.home)

    def tearDown(self):
        self._tmp.cleanup()


class HomeTests(ToolCase):
    def test_default_home_uses_the_unsuffixed_keychain_item(self):
        self.assertEqual(self.tool.keychain_service_for(None), "Claude Code-credentials")

    def test_profile_keychain_item_is_suffixed_with_the_dir_hash(self):
        profile = self.home / ".claude-profiles" / "charlie"
        digest = hashlib.sha256(str(profile).encode()).hexdigest()[:8]
        self.assertEqual(self.tool.keychain_service_for(profile), f"Claude Code-credentials-{digest}")

    def test_new_profile_links_shared_entries_and_keeps_runtime_state_local(self):
        profile = self.tool.ensure_profile("charlie")
        self.assertTrue((profile / "skills").is_symlink())
        self.assertTrue((profile / "settings.json").is_symlink())
        self.assertFalse((profile / "daemon").exists())  # per-install runtime, never shared

    def test_seeded_config_is_onboarded_but_holds_no_login(self):
        profile = self.tool.ensure_profile("charlie")
        seeded = json.loads((profile / ".claude.json").read_text())
        self.assertTrue(seeded["hasCompletedOnboarding"])
        self.assertEqual(seeded["projects"], {"/x": {}})
        self.assertNotIn("oauthAccount", seeded)
        self.assertNotIn("userID", seeded)

    def test_linking_never_replaces_what_a_profile_made_for_itself(self):
        profile = self.home / ".claude-profiles" / "charlie"
        profile.mkdir(parents=True)
        (profile / "settings.json").write_text('{"mine": true}')
        self.tool.link_profile(profile)
        self.assertFalse((profile / "settings.json").is_symlink())
        self.assertEqual(json.loads((profile / "settings.json").read_text()), {"mine": True})

    def test_home_for_finds_an_account_in_a_profile(self):
        profile = self.tool.ensure_profile("charlie")
        write_config(profile / ".claude.json", "lab@example.com")
        home = self.tool.home_for("lab@example.com")
        self.assertEqual(home.name, "charlie")
        self.assertEqual(home.env_value(), str(profile))
        self.assertTrue(self.tool.home_for("personal@example.com").is_default)
        self.assertIsNone(self.tool.home_for("work@example.com"))

    def test_accounts_resolve_by_call_sign_alias_or_email(self):
        self.tool.roster_write({"personal": {"email": "personal@example.com"}})
        self.assertEqual(self.tool.resolve_email("charlie"), "lab@example.com")
        self.assertEqual(self.tool.resolve_email("Delta"), "team@example.com")
        self.assertEqual(self.tool.resolve_email("Bravo"), "personal@example.com")
        self.assertEqual(self.tool.resolve_email("personal"), "personal@example.com")
        self.assertIsNone(self.tool.resolve_email("zulu"))


class SelectionTests(ToolCase):
    def test_without_a_selection_new_sessions_use_the_default_home(self):
        self.assertEqual(self.tool.selected_email(), "personal@example.com")
        self.assertTrue(self.tool.resolve_launch_home(None).is_default)

    def test_use_routes_new_sessions_and_journals_the_switch(self):
        profile = self.tool.ensure_profile("charlie")
        write_config(profile / ".claude.json", "lab@example.com")
        self.assertTrue(self.tool.select("lab@example.com", mode="fixed", source="test"))
        self.assertEqual(self.tool.resolve_launch_home(None).config_dir, profile)
        event = json.loads(self.tool.SWITCH_LOG.read_text().splitlines()[-1])
        self.assertEqual(event["source"], "test")
        self.assertIsNone(json.loads(self.tool.SELECTION.read_text()).get("accessToken"))

    def test_reselecting_the_same_account_journals_nothing(self):
        self.tool.select("personal@example.com", mode="fixed", source="test")
        self.assertFalse(self.tool.SWITCH_LOG.exists())

    def test_a_selection_whose_login_is_gone_falls_back_to_the_default_home(self):
        self.tool.SELECTION.parent.mkdir(parents=True, exist_ok=True)
        self.tool.SELECTION.write_text(json.dumps({"email": "lab@example.com", "mode": "fixed"}))
        self.assertEqual(self.tool.selected_email(), "personal@example.com")

    def test_one_launch_override_names_an_account_without_a_login(self):
        with mock.patch.dict("os.environ", {"CLAUDE_ACCOUNT": "charlie"}):
            with self.assertRaisesRegex(RuntimeError, "claude-account login charlie"):
                self.tool.resolve_launch_home(None)

    def test_auto_mode_asks_glideslope_and_moves_the_pointer(self):
        profile = self.tool.ensure_profile("charlie")
        write_config(profile / ".claude.json", "lab@example.com")
        self.tool.SELECTION.parent.mkdir(parents=True, exist_ok=True)
        self.tool.SELECTION.write_text(json.dumps({"mode": "auto"}))
        pick = {"email": "lab@example.com", "reason": "Bravo is over the line"}
        with mock.patch.object(self.tool, "glideslope_pick", return_value=pick):
            home = self.tool.resolve_launch_home(None)
        self.assertEqual(home.name, "charlie")
        stored = json.loads(self.tool.SELECTION.read_text())
        self.assertEqual((stored["email"], stored["mode"]), ("lab@example.com", "auto"))


if __name__ == "__main__":
    unittest.main()


class UsageBackoffTests(ToolCase):
    """A 429 pauses every usage read on the machine; reads in one run are spaced."""

    USAGE = {"limits": []}

    def setUp(self):
        super().setUp()
        profile = self.tool.ensure_profile("charlie")
        write_config(profile / ".claude.json", "lab@example.com")
        self.tool.roster_write({"lab": {"email": "lab@example.com"},
                                "personal": {"email": "personal@example.com"}})

    def status(self, fetch):
        import contextlib, io
        out = io.StringIO()
        with mock.patch.object(self.tool, "live_token", return_value="token"), \
             mock.patch.object(self.tool, "fetch_usage", side_effect=fetch) as fetched, \
             mock.patch.object(self.tool.time, "sleep") as slept, \
             contextlib.redirect_stdout(out):
            self.tool.cmd_status(["--json"])
        return json.loads(out.getvalue()), fetched, slept

    def test_reads_in_one_run_are_spaced(self):
        report, fetched, slept = self.status(lambda token: self.USAGE)
        self.assertEqual(fetched.call_count, 2)
        slept.assert_called_once_with(self.tool.READ_SPACING_S)
        self.assertFalse(report["lab"].get("stale"))

    def test_a_429_pauses_the_rest_of_the_run_and_records_the_pause(self):
        def throttled(token):
            raise self.tool.Throttled(120.0)
        report, fetched, _ = self.status(throttled)
        self.assertEqual(fetched.call_count, 1)
        self.assertTrue(all(row["stale"] for row in report.values()))
        self.assertIn("throttled", report["lab"]["error"])
        self.assertIsNotNone(self.tool.backoff_until())

    def test_an_active_pause_sends_no_request(self):
        self.tool.start_backoff(None)
        report, fetched, _ = self.status(lambda token: self.USAGE)
        self.assertEqual(fetched.call_count, 0)
        self.assertIn("next read after", report["personal"]["error"])

    def test_the_pause_is_bounded(self):
        now = 1_000_000.0
        self.assertEqual(self.tool.start_backoff(5, now=now), now + self.tool.BACKOFF_MIN_S)
        self.assertEqual(self.tool.start_backoff(10**6, now=now), now + self.tool.BACKOFF_MAX_S)
        self.assertEqual(self.tool.start_backoff(None, now=now), now + self.tool.BACKOFF_DEFAULT_S)
        self.assertIsNone(self.tool.backoff_until(now=now + self.tool.BACKOFF_DEFAULT_S + 1))


class MeterTokenTests(ToolCase):
    """An account nobody is logged into is read through an operator-minted token, by org ID."""

    HEADERS = {
        "anthropic-organization-id": "org-lab",
        "anthropic-ratelimit-unified-5h-utilization": "0.14",
        "anthropic-ratelimit-unified-5h-reset": "1790569200",
        "anthropic-ratelimit-unified-7d-utilization": "0.13",
        "anthropic-ratelimit-unified-7d-reset": "1790845200",
        "anthropic-ratelimit-unified-7d_oi-utilization": "0.02",
        "anthropic-ratelimit-unified-7d_oi-reset": "1790845200",
    }

    def setUp(self):
        super().setUp()
        self.tool.roster_write({"lab": {"email": "lab@example.com", "org_uuid": "org-lab"},
                                "personal": {"email": "personal@example.com", "org_uuid": "org-personal"}})
        self.tool.METER_TOKENS.mkdir(parents=True)

    def token(self, name, value="sk-ant-oat01-secret"):
        path = self.tool.METER_TOKENS / name
        path.write_text(f"# whatever a human typed here\n{value}\n")
        return path

    def run_cmd(self, fn, args, probe):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(self.tool, "probe_token", side_effect=probe), \
             mock.patch.object(self.tool, "live_token", side_effect=RuntimeError("expired")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fn(args)
            except SystemExit as exc:
                code = exc.code or 0
        return out.getvalue(), err.getvalue(), code

    def test_headers_become_the_usage_reads_rows_and_an_absent_claim_is_unknown(self):
        rows = self.tool.meter_rows(self.HEADERS)
        self.assertEqual([(r["label"], r["percent"]) for r in rows],
                         [("session (5h)", 14.0), ("weekly (all models)", 13.0), ("weekly (Fable)", 2.0)])
        self.assertTrue(rows[1]["resets_at"].startswith("2026-10-01T09:00:00"))
        partial = {k: v for k, v in self.HEADERS.items() if "7d_oi" not in k}
        self.assertNotIn("weekly (Fable)", [r["label"] for r in self.tool.meter_rows(partial)])

    def test_status_reads_an_account_no_home_holds(self):
        self.token("lab.token")
        out, _, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                 lambda token: ("org-lab", self.tool.meter_rows(self.HEADERS)))
        row = json.loads(out)["lab"]
        self.assertEqual(row["source"], "meter-token")
        self.assertFalse(row.get("stale"))
        self.assertFalse(row["logged_in"])
        self.assertEqual(row["limits"][1]["percent"], 13.0)
        self.assertNotIn("secret", out)

    def test_an_unknown_org_is_warned_and_never_credited(self):
        self.token("lab.token")
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                   lambda token: ("org-stranger", self.tool.meter_rows(self.HEADERS)))
        self.assertIn("org-stranger", err)
        self.assertNotEqual(json.loads(out)["lab"].get("source"), "meter-token")

    def test_a_token_named_for_one_account_that_bills_another_says_so(self):
        """The 2026-09-25 failure: a file called charlie.token that billed Bravo."""
        self.token("charlie.token")  # Charlie is lab@example.com in the fixture call-signs
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                   lambda token: ("org-personal", self.tool.meter_rows(self.HEADERS)))
        self.assertIn("named for Charlie but bills Bravo", err)
        self.assertEqual(json.loads(out)["personal"]["read_via"], "charlie.token")

    def test_whose_verifies_against_the_expected_account(self):
        path = str(self.token("seat.token"))
        probe = lambda token: ("org-personal", self.tool.meter_rows(self.HEADERS))
        out, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--expect", "bravo", "--json"], probe)
        self.assertEqual(code, 0)
        row = json.loads(out)[0]
        self.assertEqual((row["call_sign"], row["verified"]), ("Bravo", True))
        self.assertNotIn("secret", out)
        _, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--expect", "charlie", "--json"], probe)
        self.assertEqual(code, 3)
        _, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--json"],
                                  lambda token: ("org-stranger", []))
        self.assertEqual(code, 4)

    def test_a_login_corrects_a_wrong_note_loudly(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.tool.roster_note({"emailAddress": "lab@example.com", "organizationUuid": "org-real"})
        self.assertEqual(self.tool.roster_read()["lab"]["org_uuid"], "org-real")
        self.assertIn("login says", err.getvalue())

    def test_note_refuses_an_org_another_account_owns(self):
        _, err, code = self.run_cmd(self.tool.cmd_note,
                                    ["work@example.com", "--org", "org-lab"], lambda token: None)
        self.assertEqual(code, 1)
        self.assertIn("already belongs", err)
