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
