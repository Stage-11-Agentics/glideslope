"""Hermetic ground for every test: a pinned config and a throwaway store.

Set before any test module imports glideslope, so the module-level constants
(timezone, satellite names, call-signs, store paths) come from the fixture and
never from the developer's own ~/.glideslope. The store points at a fresh
temporary directory, so no test can read a real sample DB or write into one.
"""
import os
import tempfile
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["GLIDESLOPE_CONFIG"] = str(FIXTURES / "config.toml")
os.environ["GLIDESLOPE_HOME"] = tempfile.mkdtemp(prefix="glideslope-tests-")

import glideslope
import claude_account

TEST_ROSTER = Path(os.environ["GLIDESLOPE_HOME"]) / "claude-roster.json"
TEST_ROSTER.write_text(json.dumps({
    "work": {"email": "work@example.test"},
    "personal": {"email": "personal@example.test"},
}), encoding="utf-8")
glideslope.CLAUDE_ROSTER = TEST_ROSTER
claude_account.SWITCH_LOG = Path(os.environ["GLIDESLOPE_HOME"]) / "switch-log.jsonl"
