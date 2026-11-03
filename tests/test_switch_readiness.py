import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glideslope


def claude(display, held_on=(), dormant=False):
    return {"provider": "Claude", "display": display, "held_on": list(held_on), "dormant": dormant}


class SwitchReadinessTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(glideslope, "LOCAL_SATELLITE", "laptop")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_here_and_declared_is_ready(self):
        labels = {"Use Echo", "Log in Echo", "Move all Echo"}
        [account] = glideslope.mark_switch_readiness([claude("Echo", ["laptop"])], labels)
        self.assertEqual(account["switch"], {
            "ready": True, "here": True,
            "declared": {"Use": True, "Log in": True, "Move all": True}, "problems": [],
            "meter": None, "complete": True, "setup": []})

    def test_no_login_here_blocks(self):
        [account] = glideslope.mark_switch_readiness([claude("Echo", ["studio"])], None)
        self.assertFalse(account["switch"]["ready"])
        self.assertIsNone(account["switch"]["declared"])
        self.assertIn("no login on laptop", account["switch"]["problems"][0])

    def test_undeclared_action_blocks(self):
        [account] = glideslope.mark_switch_readiness(
            [claude("Echo", ["laptop"])], {"Use Delta", "Log in Delta", "Move all Delta"})
        self.assertFalse(account["switch"]["ready"])
        self.assertEqual(account["switch"]["declared"],
                         {"Use": False, "Log in": False, "Move all": False})
        self.assertIn('"Use Echo"', account["switch"]["problems"][0])

    def test_gaps_skip_dormant_and_complete(self):
        accounts = glideslope.mark_switch_readiness(
            [claude("Alpha", ["laptop"]), claude("Bravo"), claude("Charlie", dormant=True),
             {"provider": "Codex", "display": "Codex"}], None)
        self.assertEqual([line.split(":")[0] for line in glideslope.setup_gaps(accounts)],
                         ["Bravo"])
        self.assertNotIn("switch", accounts[3])

    def test_no_meter_token_is_a_gap_but_not_a_switch_blocker(self):
        account = dict(claude("Echo", ["laptop"]), meter_token=False)
        [account] = glideslope.mark_switch_readiness([account], None)
        self.assertTrue(account["switch"]["ready"])
        self.assertFalse(account["switch"]["complete"])
        self.assertIn("no meter token", account["switch"]["setup"][0])

    def test_meter_tokens_join_by_login_and_only_keepers_speak(self):
        def acct(email, org):
            return {"provider": "Claude", "display": email[0].upper(), "email": email,
                    "login": f"{email}#{org}"}
        keeper = {"name": "studio", "snapshot": {
            "a": {"email": "a@example.com", "org_uuid": "org-a", "meter_token": True},
            "b": {"email": "b@example.com", "org_uuid": "org-b", "meter_token": False}}}
        plain = {"name": "spare", "snapshot": {"a": {"email": "a@example.com", "org_uuid": "org-a"}}}
        accounts = [acct("a@example.com", "org-a"), acct("b@example.com", "org-b"), acct("c@example.com", "org-c")]
        glideslope.mark_meter_tokens(accounts, None, [plain, keeper])
        self.assertEqual([(a["meter_token"], a["meter_on"]) for a in accounts],
                         [(True, ["studio"]), (False, []), (False, [])])
        glideslope.mark_meter_tokens(accounts, None, [plain])
        self.assertEqual([a["meter_token"] for a in accounts], [None, None, None])
        self.assertIsNone(glideslope.meter_token_logins(plain["snapshot"]))
        glideslope.mark_meter_tokens(accounts, ["c@example.com#org-c"], [])
        self.assertEqual([a["meter_on"] for a in accounts], [[], [], ["laptop"]])

    def test_a_silent_keeper_reads_unknown_and_a_plain_satellite_is_forgotten(self):
        account = {"provider": "Claude", "display": "A", "login": "a@example.com#org-a"}
        down = glideslope.mark_meter_tokens([account], None, [], {"studio"})
        self.assertEqual((account["meter_token"], account["meter_silent"], down),
                         ("unknown", ["studio"], {"studio"}))
        plain = {"name": "studio", "snapshot": {"a": {"email": "a@example.com", "org_uuid": "org-a"}}}
        self.assertEqual(glideslope.mark_meter_tokens([account], None, [plain], {"studio"}), set())
        self.assertIsNone(account["meter_token"])
        glideslope.mark_meter_tokens([account], None, [], {"laptop"}, local_read=False)
        self.assertEqual(account["meter_token"], "unknown")  # this machine keeps them, its read failed
        [ready] = glideslope.mark_switch_readiness([dict(claude("A", ["laptop"]), meter_token="unknown")], None)
        self.assertTrue(ready["switch"]["complete"])

    def test_one_email_two_plans_join_by_organization(self):
        bravo = {"provider": "Claude", "display": "Bravo", "login": "shared@example.com#org-b"}
        delta = {"provider": "Claude", "display": "Delta", "login": "shared@example.com#org-d"}
        keeper = {"name": "studio", "snapshot": {
            "b": {"email": "shared@example.com", "org_uuid": "org-b", "meter_token": True}}}
        glideslope.mark_meter_tokens([bravo, delta], None, [keeper])
        self.assertEqual((bravo["meter_token"], delta["meter_token"]), (True, False))

    def test_manifest_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.toml"
            path.write_text('[[action]]\nlabel = "Use Echo"\ncommand = "x"\n\n'
                            '[[action]]\nlabel = "Refresh"\n')
            self.assertEqual(glideslope.manifest_action_labels(path), {"Use Echo", "Refresh"})
            self.assertIsNone(glideslope.manifest_action_labels(Path(tmp) / "absent.toml"))
        self.assertIsNone(glideslope.manifest_action_labels(None))


if __name__ == "__main__":
    unittest.main()
