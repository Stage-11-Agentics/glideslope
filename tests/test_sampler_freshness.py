"""Regression coverage for the sampler-to-deck freshness handoff."""

import json
import unittest
from unittest.mock import patch

import sampler


class SamplerDeckFreshnessTests(unittest.TestCase):
    def test_rebuild_deck_reuses_exact_sample_without_another_provider_read(self):
        position = {
            "generated_at": "2026-07-25T22:49:33Z",
            "accounts": [{"provider": "Claude", "display": "Bravo", "active": True}],
        }

        with patch.object(sampler.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "deck built"
            run.return_value.stderr = ""
            self.assertEqual(sampler.rebuild_deck(position), "deck built")

        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [sampler.sys.executable, str(sampler.DECK_BUILDER), "--position-stdin"],
        )
        self.assertEqual(json.loads(kwargs["input"]), position)
        self.assertNotIn("--fresh", args[0])
        self.assertTrue(kwargs["capture_output"])

    def test_rebuild_failure_is_visible_to_the_sampler(self):
        with patch.object(sampler.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "builder stopped for private@example.com"
            run.return_value.stdout = ""
            with self.assertRaisesRegex(RuntimeError, r"builder stopped for \[redacted-email\]"):
                sampler.rebuild_deck({"accounts": []})


if __name__ == "__main__":
    unittest.main()
