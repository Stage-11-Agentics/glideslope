"""Hermetic ground for every test: a pinned config and a throwaway store.

Set before any test module imports glideslope, so the module-level constants
(timezone, satellite names, call-signs, store paths) come from the fixture and
never from the developer's own ~/.glideslope. The store points at a fresh
temporary directory, so no test can read a real sample DB or write into one.
"""
import os
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
os.environ["GLIDESLOPE_CONFIG"] = str(FIXTURES / "config.toml")
os.environ["GLIDESLOPE_HOME"] = tempfile.mkdtemp(prefix="glideslope-tests-")
