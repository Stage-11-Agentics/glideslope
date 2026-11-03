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
            "declared": {"Use": True, "Log in": True, "Move all": True}, "problems": []})

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

    def test_blockers_skip_dormant_and_ready(self):
        accounts = glideslope.mark_switch_readiness(
            [claude("Alpha", ["laptop"]), claude("Bravo"), claude("Charlie", dormant=True),
             {"provider": "Codex", "display": "Codex"}], None)
        self.assertEqual([line.split(":")[0] for line in glideslope.switch_blockers(accounts)],
                         ["Bravo"])
        self.assertNotIn("switch", accounts[3])

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
