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
        self.assertEqual(self.tool.resolve_login("charlie"), "lab@example.com")
        self.assertEqual(self.tool.resolve_login("Delta"), "team@example.com")
        self.assertEqual(self.tool.resolve_login("Bravo"), "personal@example.com")
        self.assertEqual(self.tool.resolve_login("personal"), "personal@example.com")
        self.assertIsNone(self.tool.resolve_login("zulu"))


class SelectionTests(ToolCase):
    def test_without_a_selection_new_sessions_use_the_default_home(self):
        self.assertEqual(self.tool.selected_login(), "personal@example.com")
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
        self.assertEqual(self.tool.selected_login(), "personal@example.com")

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


def write_login(path: Path, email: str, org: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email, "organizationUuid": org}}))


class TwoOrganizationsOneEmailTests(ToolCase):
    """One sign-in, two subscriptions: each organization is its own account (2026-10-30)."""

    def setUp(self):
        super().setUp()
        write_login(self.home / ".claude.json", "same@example.com", "ORG-NEW")
        self.profile = self.tool.ensure_profile("bravo")
        write_login(self.profile / ".claude.json", "same@example.com", "org-old")
        self.tool.roster_write({
            "old": {"email": "same@example.com", "org_uuid": "org-old"},
            "new": {"email": "same@example.com", "org_uuid": "org-new"},
        })
        self.tool.CALL_SIGNS = self.tool.call_signs_by_login(configured={"old": "Bravo", "new": "Delta"})

    def test_login_ids_carry_the_organization_and_a_bare_email_matches_either(self):
        self.assertEqual(self.tool.login_id("Same@Example.com", "ORG-NEW"), "same@example.com#org-new")
        self.assertTrue(self.tool.same_login("same@example.com", "same@example.com#org-old"))
        self.assertFalse(self.tool.same_login("same@example.com#org-new", "same@example.com#org-old"))

    def test_each_organization_has_its_own_call_sign_home_and_alias(self):
        self.assertEqual(self.tool.resolve_login("delta"), "same@example.com#org-new")
        self.assertTrue(self.tool.home_for("same@example.com#org-new").is_default)
        self.assertEqual(self.tool.home_for(self.tool.resolve_login("bravo")).name, "bravo")
        self.assertEqual(self.tool.label_for("same@example.com#org-old"), "Bravo")
        self.assertIsNone(self.tool.call_sign_for("same@example.com"))  # ambiguous, never guessed
        self.assertEqual(self.tool.alias_for("same@example.com#org-new", self.tool.roster_read()), "new")
        self.assertIsNone(self.tool.alias_for("same@example.com", self.tool.roster_read()))

    def test_use_routes_by_organization_and_records_it(self):
        self.assertTrue(self.tool.select(self.tool.resolve_login("bravo"), mode="fixed", source="test"))
        self.assertEqual(self.tool.resolve_launch_home(None).config_dir, self.profile)
        stored = json.loads(self.tool.SELECTION.read_text())
        self.assertEqual((stored["email"], stored["org_uuid"]), ("same@example.com", "org-old"))
        event = json.loads(self.tool.SWITCH_LOG.read_text().splitlines()[-1])
        self.assertEqual((event["from"], event["to"]), ("new", "old"))

    def test_an_email_only_selection_resolves_to_a_concrete_home(self):
        self.tool.SELECTION.parent.mkdir(parents=True, exist_ok=True)
        self.tool.SELECTION.write_text(json.dumps({"email": "same@example.com", "mode": "fixed"}))
        self.assertEqual(self.tool.selected_login(), "same@example.com#org-new")

    def test_status_reads_each_organization_once_and_never_mixes_them(self):
        import contextlib, io
        out = io.StringIO()
        reads = {"default": ({"limits": [{"kind": "session", "percent": 1}]}, "org-new"),
                 "bravo": ({"limits": [{"kind": "session", "percent": 60}]}, "org-old")}
        with mock.patch.object(self.tool, "live_token", side_effect=lambda home: home.name), \
             mock.patch.object(self.tool, "fetch_usage", side_effect=lambda token: reads[token]), \
             mock.patch.object(self.tool, "meter_reads", return_value=({}, [])), \
             mock.patch.object(self.tool.time, "sleep"), contextlib.redirect_stdout(out):
            self.tool.cmd_status(["--json"])
        report = json.loads(out.getvalue())
        self.assertEqual(set(report), {"old", "new"})
        self.assertEqual((report["new"]["home"], report["new"]["limits"][0]["percent"]), ("default", 1.0))
        self.assertEqual((report["old"]["home"], report["old"]["limits"][0]["percent"]), ("bravo", 60.0))
        self.assertEqual(report["new"]["org_uuid"], "org-new")
        self.assertTrue(report["new"]["active"])
        self.assertFalse(report["old"]["active"])
        self.assertEqual(set(self.tool.roster_read()), {"old", "new"})  # no flip-flop, no stray entry

    def test_an_email_only_journal_is_refused_once_two_accounts_share_the_email(self):
        self.tool.cache_put("old", "same@example.com", [("session (5h)", 9.0, None)])
        self.assertIsNone(self.tool.cache_owned("old", self.tool.roster_read()))
        self.tool.cache_put("old", "same@example.com", [("session (5h)", 9.0, None)], org_uuid="org-old")
        self.assertIsNotNone(self.tool.cache_owned("old", self.tool.roster_read()))
        self.tool.cache_put("new", "same@example.com", [("session (5h)", 9.0, None)], org_uuid="org-old")
        self.assertIsNone(self.tool.cache_owned("new", self.tool.roster_read()))

    def test_a_login_without_an_organization_never_mints_entries(self):
        for _ in range(3):
            self.assertIsNone(self.tool.roster_note({"emailAddress": "same@example.com"}))
        self.assertEqual(set(self.tool.roster_read()), {"old", "new"})

    def test_saving_under_another_organizations_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "pick another name"):
            self.tool.roster_note({"emailAddress": "same@example.com", "organizationUuid": "org-new"}, "old")
        self.assertEqual(self.tool.roster_read()["old"]["org_uuid"], "org-old")

    def test_an_email_keyed_call_sign_names_no_one_when_two_accounts_share_the_email(self):
        signs = self.tool.call_signs_by_login(configured={"same@example.com": "Bravo"})
        self.assertEqual(signs, {})

    def test_a_failed_read_with_a_refused_journal_still_carries_a_limits_list(self):
        import contextlib, io
        self.tool.cache_put("old", "same@example.com", [("session (5h)", 9.0, None)])  # email only: refused
        out = io.StringIO()

        def live(home):
            if home.name == "bravo":
                raise RuntimeError("expired")
            return home.name

        with mock.patch.object(self.tool, "live_token", side_effect=live), \
             mock.patch.object(self.tool, "fetch_usage", return_value=({"limits": []}, "org-new")), \
             mock.patch.object(self.tool, "meter_reads", return_value=({}, [])), \
             mock.patch.object(self.tool.time, "sleep"), contextlib.redirect_stdout(out):
            self.tool.cmd_status(["--json"])
        report = json.loads(out.getvalue())
        self.assertEqual((report["old"]["limits"], report["old"]["stale"]), ([], True))
        self.assertIn("expired", report["old"]["error"])

    def test_saving_under_a_name_renames_an_auto_named_account(self):
        self.tool.roster_write({"old": {"email": "same@example.com", "org_uuid": "org-old"}})
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            auto = self.tool.roster_note({"emailAddress": "same@example.com", "organizationUuid": "org-new"})
            renamed = self.tool.roster_note({"emailAddress": "same@example.com", "organizationUuid": "org-new"},
                                            "delta")
        roster = self.tool.roster_read()
        self.assertNotIn(auto, roster)
        self.assertEqual((renamed, roster["delta"]["org_uuid"]), ("delta", "org-new"))
        self.assertEqual(roster["old"]["org_uuid"], "org-old")


class MidSwitchTests(ToolCase):
    """A home's login record and its credential disagree mid-/login (2026-11-01).

    The default home's record names Delta (org-new) while its keychain token is Bravo's
    (org-old), as when a session still running there writes its old record back over a
    fresh /login. The usage endpoint answers for the token, and names its organization in
    `anthropic-organization-id`. Only the HTTP layer is faked: fetch_usage runs for real.
    """

    BRAVO_METERS = {"limits": [
        {"kind": "weekly_all", "percent": 83, "resets_at": "2026-11-05T09:00:00+00:00"},
        {"kind": "weekly_scoped", "percent": 76, "resets_at": "2026-11-05T09:00:00+00:00",
         "scope": {"model": {"display_name": "Fable"}}},
    ]}
    DELTA_LAST = [("weekly (all models)", 99.0, "2026-11-03T08:00:00+00:00"),
                  ("weekly (Fable)", 95.0, "2026-11-03T08:00:00+00:00")]

    def setUp(self):
        super().setUp()
        write_login(self.home / ".claude.json", "same@example.com", "org-new")  # the stale record
        self.profile = self.tool.ensure_profile("bravo")
        write_login(self.profile / ".claude.json", "same@example.com", "org-old")
        self.tool.roster_write({
            "old": {"email": "same@example.com", "org_uuid": "org-old"},
            "new": {"email": "same@example.com", "org_uuid": "org-new"},
        })
        self.tool.CALL_SIGNS = self.tool.call_signs_by_login(configured={"old": "Bravo", "new": "Delta"})
        self.tool.cache_put("new", "same@example.com", self.DELTA_LAST, org_uuid="org-new")

    def status(self, response_org):
        import contextlib, http.client, io

        class Response:
            def __init__(self, body, org):
                self.body, self.headers = body, http.client.HTTPMessage()
                if org:
                    self.headers["anthropic-organization-id"] = org

            def read(self):
                return json.dumps(self.body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def server(request, timeout=None):
            # The token decides whose meters come back, whatever the home's record says.
            self.assertEqual(request.get_header("Authorization"), "Bearer default-home-token")
            return Response(self.BRAVO_METERS, response_org)

        def token(home):
            if home.is_default:
                return "default-home-token"
            raise RuntimeError("the bravo home's access token has expired")

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.tool, "live_token", side_effect=token), \
             mock.patch.object(self.tool.urllib.request, "urlopen", side_effect=server), \
             mock.patch.object(self.tool, "meter_reads", return_value=({}, [])), \
             mock.patch.object(self.tool.time, "sleep"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.tool.cmd_status(["--json"])
        return json.loads(out.getvalue()), err.getvalue()

    def percents(self, rows):
        return [(row["label"], row["percent"]) for row in rows]

    def test_a_read_is_credited_to_the_organization_the_response_names(self):
        report, err = self.status("org-old")
        # Delta keeps its own last reading, marked stale; Bravo's meters never land on it.
        self.assertTrue(report["new"].get("stale"))
        self.assertEqual(self.percents(report["new"]["limits"]),
                         [("weekly (all models)", 99.0), ("weekly (Fable)", 95.0)])
        self.assertEqual(self.percents(self.tool.cache_get("new")["limits"]),
                         [("weekly (all models)", 99.0), ("weekly (Fable)", 95.0)])
        self.assertIn("Bravo's credential", report["new"]["error"])
        self.assertIn("Bravo's credential", err)
        # Bravo gets them, through the home that actually holds its token.
        self.assertFalse(report["old"].get("stale"))
        self.assertEqual(self.percents(report["old"]["limits"]),
                         [("weekly (all models)", 83.0), ("weekly (Fable)", 76.0)])
        self.assertEqual((report["old"]["read_org_uuid"], report["old"]["read_via"]),
                         ("org-old", "home default"))
        self.assertEqual(self.tool.cache_get("old")["org_uuid"], "org-old")

    def test_a_read_naming_no_organization_is_credited_to_neither_account_under_the_email(self):
        report, err = self.status(None)
        self.assertTrue(report["new"].get("stale"))
        self.assertEqual(self.percents(self.tool.cache_get("new")["limits"]),
                         [("weekly (all models)", 99.0), ("weekly (Fable)", 95.0)])
        self.assertTrue(report["old"].get("stale"))
        self.assertIsNone(self.tool.cache_get("old"))
        self.assertIn("named no organization", err)

    def test_a_read_from_an_organization_the_roster_lacks_is_credited_to_none(self):
        report, err = self.status("org-elsewhere")
        self.assertTrue(report["new"].get("stale"))
        self.assertTrue(report["old"].get("stale"))
        self.assertIsNone(self.tool.cache_get("old"))
        self.assertIn("no roster entry holds", err)


if __name__ == "__main__":
    unittest.main()


class ExtraUsageParseTests(ToolCase):
    """The month-to-date counter on the usage payload. A missing block is not zero."""

    def test_a_null_monthly_limit_still_yields_the_spend(self):
        parsed = self.tool.parse_extra_usage({
            "extra_usage": {
                "is_enabled": True, "monthly_limit": None, "used_credits": 1820,
                "utilization": None, "currency": "usd",
            },
        })
        self.assertEqual(parsed, {
            "enabled": True, "used_minor": 1820, "currency": "USD", "exponent": 2,
        })

    def test_spend_wins_and_names_its_exponent(self):
        parsed = self.tool.parse_extra_usage({
            "spend": {
                "enabled": True,
                "used": {"amount_minor": 5000, "currency": "USD", "exponent": 2},
            },
            "extra_usage": {"is_enabled": True, "used_credits": 1, "currency": "EUR"},
        })
        self.assertEqual(parsed["used_minor"], 5000)
        self.assertEqual(parsed["currency"], "USD")
        self.assertEqual(parsed["exponent"], 2)

    def test_a_broken_spend_block_falls_through_and_decimal_places_are_honored(self):
        parsed = self.tool.parse_extra_usage({
            "spend": {"enabled": True},
            "extra_usage": {
                "is_enabled": True, "used_credits": 5000, "decimal_places": 3, "currency": "GBP",
            },
        })
        self.assertEqual(parsed["used_minor"], 5000)
        self.assertEqual(parsed["exponent"], 3)
        self.assertEqual(parsed["currency"], "GBP")

    def test_amount_minor_units_is_the_fallback_name(self):
        parsed = self.tool.parse_extra_usage({
            "spend": {"enabled": True, "used": {"amount_minor_units": 80, "currency": "EUR", "exponent": 2}},
        })
        self.assertEqual(parsed["used_minor"], 80)
        self.assertEqual(parsed["currency"], "EUR")

    def test_disabled_zero_is_a_reading_and_garbage_is_not(self):
        zero = self.tool.parse_extra_usage({
            "extra_usage": {"is_enabled": False, "used_credits": 0, "currency": "USD"},
        })
        self.assertEqual(zero["used_minor"], 0)
        self.assertFalse(zero["enabled"])
        self.assertIsNone(self.tool.parse_extra_usage({"limits": []}))
        self.assertIsNone(self.tool.parse_extra_usage({
            "extra_usage": {"is_enabled": True, "used_credits": -5, "currency": "USD"},
        }))
        self.assertIsNone(self.tool.parse_extra_usage({"extra_usage": {"is_enabled": True}}))


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
             mock.patch.object(self.tool, "fetch_usage",
                               side_effect=lambda token: (fetch(token), None)) as fetched, \
             mock.patch.object(self.tool.time, "sleep") as slept, \
             contextlib.redirect_stdout(out):
            self.tool.cmd_status(["--json"])
        return json.loads(out.getvalue()), fetched, slept

    def test_a_login_read_reports_usage_credits(self):
        payload = {"limits": [], "extra_usage": {
            "is_enabled": True, "monthly_limit": None, "used_credits": 1820, "currency": "USD",
        }}
        report, _, _ = self.status(lambda token: payload)
        for alias in ("lab", "personal"):
            self.assertEqual(report[alias]["extra_usage"]["used_minor"], 1820)
            self.assertEqual(self.tool.cache_get(alias)["extra_usage"]["used_minor"], 1820)

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


class MeterCase(ToolCase):
    """A roster that knows two orgs, an empty meter-token directory, and a command runner."""

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



class MeterTokenTests(MeterCase):
    """An account nobody is logged into is read through an operator-minted token, by org ID."""

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
                                 lambda token, **_: ("org-lab", self.tool.meter_rows(self.HEADERS), None))
        row = json.loads(out)["lab"]
        self.assertEqual(row["source"], "meter-token")
        self.assertFalse(row.get("stale"))
        self.assertFalse(row["logged_in"])
        self.assertEqual(row["limits"][1]["percent"], 13.0)
        self.assertNotIn("extra_usage", row)
        self.assertNotIn("secret", out)

    def test_a_meter_read_drops_a_cached_counter_and_a_stale_login_keeps_one(self):
        self.tool.cache_put(
            "lab", "lab@example.com",
            [("session (5h)", 4.0, "2026-10-01T00:00:00Z")],
            {"enabled": True, "used_minor": 250, "currency": "USD", "exponent": 2},
        )
        out, _, _ = self.run_cmd(self.tool.cmd_status, ["--json"], lambda token, **_: None)
        stale = json.loads(out)["lab"]
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["extra_usage"]["used_minor"], 250)

        self.token("lab.token")
        out, _, _ = self.run_cmd(
            self.tool.cmd_status, ["--json"],
            lambda token, **_: ("org-lab", self.tool.meter_rows(self.HEADERS), None))
        fresh = json.loads(out)["lab"]
        self.assertEqual(fresh["source"], "meter-token")
        self.assertNotIn("extra_usage", fresh)
        self.assertNotIn("extra_usage", self.tool.cache_get("lab"))

    def test_an_unknown_org_is_warned_and_never_credited(self):
        self.token("lab.token")
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                   lambda token, **_: ("org-stranger", self.tool.meter_rows(self.HEADERS), None))
        self.assertIn("org-stranger", err)
        self.assertNotEqual(json.loads(out)["lab"].get("source"), "meter-token")

    def test_a_token_named_for_one_account_that_bills_another_says_so(self):
        """The 2026-09-25 failure: a file called charlie.token that billed Bravo."""
        self.token("charlie.token")  # Charlie is lab@example.com in the fixture call-signs
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                   lambda token, **_: ("org-personal", self.tool.meter_rows(self.HEADERS), None))
        self.assertIn("named for Charlie but bills Bravo", err)
        self.assertEqual(json.loads(out)["personal"]["read_via"], "charlie.token")

    def test_whose_verifies_against_the_expected_account(self):
        path = str(self.token("seat.token"))
        probe = lambda token, **_: ("org-personal", self.tool.meter_rows(self.HEADERS), None)
        out, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--expect", "bravo", "--json"], probe)
        self.assertEqual(code, 0)
        row = json.loads(out)[0]
        self.assertEqual((row["call_sign"], row["verified"]), ("Bravo", True))
        self.assertNotIn("secret", out)
        _, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--expect", "charlie", "--json"], probe)
        self.assertEqual(code, 3)
        _, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--json"],
                                  lambda token, **_: ("org-stranger", [], None))
        self.assertEqual(code, 4)

    def test_a_login_in_a_new_organization_is_its_own_account_loudly(self):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            alias = self.tool.roster_note({"emailAddress": "lab@example.com", "organizationUuid": "org-real"})
        roster = self.tool.roster_read()
        self.assertNotEqual(alias, "lab")
        self.assertEqual(roster["lab"]["org_uuid"], "org-lab")
        self.assertEqual(roster[alias]["org_uuid"], "org-real")
        self.assertIn("its own account", err.getvalue())

    def test_note_refuses_an_org_another_account_owns(self):
        _, err, code = self.run_cmd(self.tool.cmd_note,
                                    ["work@example.com", "--org", "org-lab"], lambda token, **_: None)
        self.assertEqual(code, 1)
        self.assertIn("already belongs", err)


class ProbeTests(ToolCase):
    """The probe's HTTP handling: what reads, what fails, and what never sends a second request."""

    HEADERS = MeterCase.HEADERS

    class Response:
        def __init__(self, headers):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def http_error(self, code, headers=None):
        import urllib.error
        return urllib.error.HTTPError("https://example.test", code, "x", headers or {}, None)

    def probe(self, *answers):
        calls = []

        def fake_open(req, timeout):
            calls.append(json.loads(req.data)["model"])
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return self.Response(answer)
        with mock.patch.object(self.tool, "_open", side_effect=fake_open), \
             mock.patch.object(self.tool, "cli_version", return_value="9.9.9"):
            try:
                return self.tool.probe_token("sk-ant-oat01-secret"), calls
            except RuntimeError as exc:
                self.assertNotIn("secret", str(exc))
                return exc, calls

    def test_a_good_answer_is_a_reading(self):
        (org, rows, fallback), calls = self.probe(self.HEADERS)
        self.assertEqual((org, len(rows), fallback, len(calls)), ("org-lab", 3, None, 1))

    def test_a_dead_token_says_so_and_stops(self):
        exc, calls = self.probe(self.http_error(401))
        self.assertIn("revoked", str(exc))
        self.assertEqual(len(calls), 1)

    def test_a_server_error_on_fable_is_a_failed_read_not_a_haiku_read(self):
        exc, calls = self.probe(self.http_error(529))
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(len(calls), 1)

    def test_a_refused_model_falls_back_to_haiku_and_says_fable_is_unread(self):
        haiku = {k: v for k, v in self.HEADERS.items() if "7d_oi" not in k}
        (org, rows, fallback), calls = self.probe(self.http_error(400), haiku)
        self.assertEqual(len(calls), 2)
        self.assertIn("refused", fallback)
        self.assertNotIn("weekly (Fable)", [r["label"] for r in rows])

    def test_verification_survives_an_overloaded_fable(self):
        """whose needs the organization, not the Fable meter: a 529 must not block a launch."""
        haiku = {k: v for k, v in self.HEADERS.items() if "7d_oi" not in k}
        calls = []

        def fake_open(req, timeout):
            calls.append(json.loads(req.data)["model"])
            if len(calls) == 1:
                raise self.http_error(529)
            return self.Response(haiku)
        with mock.patch.object(self.tool, "_open", side_effect=fake_open), \
             mock.patch.object(self.tool, "cli_version", return_value="9.9.9"):
            org, rows, fallback = self.tool.probe_token("t", identity_only=True)
        self.assertEqual((org, len(calls)), ("org-lab", 2))
        self.assertIn("529", fallback)

    def test_a_throttled_account_reads_as_throttled_and_a_bare_429_sends_nothing_more(self):
        (org, rows, _), calls = self.probe(self.http_error(429, self.HEADERS))
        self.assertEqual((org, len(calls)), ("org-lab", 1))
        exc, calls = self.probe(self.http_error(429))
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(len(calls), 1)


class MeterRobustnessTests(MeterCase):
    """One bad file, one ambiguous org, or one covered account never costs the others."""

    def test_a_malformed_token_file_costs_only_itself(self):
        (self.tool.METER_TOKENS / "bad.token").write_text("sk-ant-oat01-se​cret\n")
        (self.tool.METER_TOKENS / "worse.token").write_bytes(b"\xff\xfe\x00garbage")
        self.token("lab.token")
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"],
                                   lambda token, **_: ("org-lab", self.tool.meter_rows(self.HEADERS), None))
        self.assertEqual(json.loads(out)["lab"]["source"], "meter-token")
        self.assertIn("bad.token", err)
        self.assertIn("worse.token", err)
        self.assertNotIn("secret", err + out)

    def test_two_entries_sharing_an_org_are_ambiguous_never_guessed(self):
        roster = self.tool.roster_read()
        roster["personal"]["org_uuid"] = "org-lab"
        self.tool.roster_write(roster)
        path = str(self.token("seat.token"))
        probe = lambda token, **_: ("org-lab", self.tool.meter_rows(self.HEADERS), None)
        out, err, _ = self.run_cmd(self.tool.cmd_status, ["--json"], probe)
        self.assertIn("share", err)
        self.assertFalse(any(r.get("source") == "meter-token" for r in json.loads(out).values()))
        _, _, code = self.run_cmd(self.tool.cmd_whose, [path, "--expect", "charlie", "--json"], probe)
        self.assertEqual(code, 4)

    def test_a_token_a_login_already_covers_is_not_probed(self):
        profile = self.tool.ensure_profile("charlie")
        write_config(profile / ".claude.json", "lab@example.com")
        token = self.token("lab.token")
        fingerprint = self.tool.token_fingerprint(self.tool.read_token_file(token))
        self.tool.write_json_atomic(self.tool.METER_ORGS, {fingerprint: "org-lab"})
        import contextlib, io
        out = io.StringIO()
        with mock.patch.object(self.tool, "live_token", return_value="login"), \
             mock.patch.object(self.tool, "fetch_usage", return_value=({"limits": []}, None)), \
             mock.patch.object(self.tool, "probe_token") as probed, \
             mock.patch.object(self.tool.time, "sleep"), contextlib.redirect_stdout(out):
            self.tool.cmd_status(["--json"])
        probed.assert_not_called()
        self.assertIn("fetched_at", json.loads(out.getvalue())["lab"])

    def test_expect_needs_a_value_and_note_stores_lowercase(self):
        _, err, code = self.run_cmd(self.tool.cmd_whose, [str(self.token("x.token")), "--expect", ""],
                                    lambda token, **_: None)
        self.assertEqual(code, 1)
        self.run_cmd(self.tool.cmd_note, ["Team@Example.com", "--org", "ORG-TEAM"], lambda token, **_: None)
        entry = self.tool.roster_read()["team"]
        self.assertEqual((entry["email"], entry["org_uuid"]), ("team@example.com", "org-team"))

    def test_call_signs_read_without_tomllib(self):
        config = self.home / "config.toml"
        config.write_text('[satellite]\nname = "x"\n\n[claude]\n'
                          'call_signs = { "work@example.com" = "Alpha", personal = "Bravo" }\n\n[codex]\nplan = "p"\n')
        self.assertEqual(self.tool._call_signs_without_tomllib(config),
                         {"work@example.com": "Alpha", "personal": "Bravo"})
