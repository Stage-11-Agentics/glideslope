"""The meter-token helper's capture: hermetic, a fake child in a pseudo-terminal, no claude."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import add_meter_token  # noqa: E402

TOKEN = "sk-ant-oat01-" + "Ab3_-" * 20 + "xyz"
CHILD = """
import sys, time
w = sys.stdout.buffer
w.write(b"Visit https://claude.com/cai/oauth/authorize?code=true&state=s\\r\\n"); w.flush()
time.sleep(0.2)
w.write(b"\\x1b[32mYour token:\\x1b[0m " + {head!r}); w.flush()
time.sleep(0.4)
w.write({tail!r} + b"\\r\\nStore it safely.\\r\\n"); w.flush()
time.sleep({linger})
"""


class CaptureTests(unittest.TestCase):
    def capture(self, head, tail, linger=30):
        code = CHILD.format(head=head, tail=tail, linger=linger)
        started = time.monotonic()
        token, said = add_meter_token.capture_token([sys.executable, "-c", code], {}, ".", timeout=20)
        return token, said, time.monotonic() - started

    def test_a_token_split_across_reads_is_captured_whole(self):
        raw = TOKEN.encode()
        token, said, took = self.capture(raw[:33], raw[33:])
        self.assertEqual(token, raw)
        self.assertNotIn(TOKEN, said)
        self.assertIn("<token>", said)
        self.assertLess(took, 10)  # stopped once the token was whole, not at the child's exit

    def test_a_child_that_prints_no_token_returns_none(self):
        token, said, _ = self.capture(b"no ", b"token here", linger=0)
        self.assertIsNone(token)
        self.assertIn("no token here", said)


if __name__ == "__main__":
    unittest.main()
