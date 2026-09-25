"""Regression coverage for the Detail-view / popup-preview build split."""

import datetime as dt
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


BUILDER_PATH = Path(__file__).parents[1] / "views" / "deck-src" / "build.py"
SPEC = importlib.util.spec_from_file_location("glideslope_deck_builder", BUILDER_PATH)
assert SPEC and SPEC.loader
deck_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deck_builder)
MARKER_START = "/*__SNAPSHOT__*/"
MARKER_END = "/*__END_SNAPSHOT__*/"


class IconTests(unittest.TestCase):
    """One drawing, both pages — a self-contained page must not carry a stale copy."""

    def test_the_mark_becomes_a_usable_data_uri(self):
        uri = deck_builder.icon_data_uri()
        self.assertTrue(uri.startswith("data:image/svg+xml,%3Csvg"))
        # a raw #, <, > or " in an href would end the attribute or the tag
        self.assertNotIn("#", uri)
        self.assertNotIn('"', uri)
        self.assertNotIn("<", uri)
        self.assertIn("viewBox", uri)

    def test_a_page_with_nowhere_to_wear_it_fails_the_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "no-icon.tmpl.html"
            template.write_text(f"<html>{MARKER_START}null{MARKER_END}</html>")
            with self.assertRaises(SystemExit):
                deck_builder.render_page(template, "null", "data:image/svg+xml,x")

    def test_both_built_pages_wear_the_same_mark(self):
        # Rendered here from the templates: the built pages are not in the repo.
        icon = deck_builder.icon_data_uri()
        views = BUILDER_PATH.parents[1]
        pages = [deck_builder.render_page(views / "deck-src" / "deck.tmpl.html", "null", icon),
                 deck_builder.render_page(views / "popup-src" / "popup.tmpl.html", "null", icon)]
        marks = []
        for page in pages:
            self.assertNotIn(deck_builder.ICON_TOKEN, page)
            start = page.index('<link rel="icon"')
            marks.append(page[start:page.index(">", start)])
        self.assertEqual(marks[0], marks[1])
        self.assertIn(deck_builder.icon_data_uri(), marks[0])


class DeckTrailTests(unittest.TestCase):
    def test_real_samples_keep_their_observation_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "samples.db"
            con = sqlite3.connect(database)
            con.execute(
                "CREATE TABLE samples ("
                "ts TEXT NOT NULL, provider TEXT NOT NULL, account TEXT NOT NULL,"
                "meter TEXT NOT NULL, used_percent REAL, observed_at TEXT NOT NULL)"
            )
            observed = "2026-07-25T06:00:00Z"
            con.execute(
                "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                (observed, "Codex", "codex", "codex", 7.0, observed),
            )
            con.commit()
            con.close()

            accounts = [{
                "provider": "Codex",
                "account": "codex",
                "display": "Codex",
                "limits": [{
                    "meter_id": "codex",
                    "window_minutes": 7 * 24 * 60,
                    "resets_at": "2026-08-01T00:00:00Z",
                }],
            }]
            with patch.object(deck_builder, "DB_PATH", database):
                trails, _ = deck_builder.trails_from_store(accounts)

        point = trails["Codex/codex"][0]
        expected_ms = round(
            dt.datetime(2026, 7, 25, 6, tzinfo=dt.timezone.utc).timestamp() * 1000
        )
        self.assertEqual(len(point), 3)
        self.assertEqual(point[0], expected_ms)
        self.assertEqual(point[2], 7.0)


class BuiltViewTests(unittest.TestCase):
    def position(self, session_percent: float) -> dict:
        return {
            "accounts": [{
                "provider": "Claude",
                "account": "private-alias",
                "display": "Bravo",
                "active": True,
                "plan": "Max 20x",
                "stale": False,
                "observed_at": "2026-07-26T00:20:23Z",
                "limits": [{
                    "meter_id": "session",
                    "label": "5h session",
                    "used_percent": session_percent,
                    "window_minutes": 300,
                    "resets_at": "2026-07-26T01:30:00Z",
                    "anchored": True,
                }],
            }],
            "openrouter": None,
            "switches": [{"from": "private-alias", "to": "other-private-alias"}],
            "warnings": [],
        }

    @staticmethod
    def embedded(page: str) -> dict:
        return json.loads(page.split(MARKER_START, 1)[1].split(MARKER_END, 1)[0])

    def test_one_snapshot_builds_rich_detail_and_compact_popup_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            detail = root / "deck.html"
            popup = root / "popup.html"
            with (
                patch.object(deck_builder, "DB_PATH", root / "missing.db"),
                redirect_stdout(StringIO()),
            ):
                snapshot = deck_builder.write_deck(
                    self.position(99.0), detail, popup
                )
            detail_page = detail.read_text()
            popup_page = popup.read_text()
            detail_snapshot = self.embedded(detail_page)
            popup_snapshot = self.embedded(popup_page)

        self.assertEqual(snapshot["accounts"][0]["limits"][0]["used_percent"], 99.0)
        self.assertEqual(detail_snapshot, popup_snapshot)
        self.assertEqual(detail_snapshot["accounts"][0]["limits"][0]["used_percent"], 99.0)
        self.assertIn("trails", detail_snapshot)
        self.assertIn("switches", detail_snapshot)
        self.assertNotIn("private-alias", detail_page + popup_page)
        self.assertIn("<title>Glideslope — Detail View</title>", detail_page)
        self.assertIn("<title>Glideslope — Popup</title>", popup_page)
        self.assertIn("DETAIL VIEW →", popup_page)
        self.assertNotIn(">DECK", popup_page)
        self.assertIn('dataset.reloadOnReturn = "true"', detail_page)
        self.assertIn('dataset.reloadOnReturn = "true"', popup_page)


class DormantAndPoolFableTests(unittest.TestCase):
    """Coverage for the two additions to the JSON contract: a dormant Claude
    account (no active subscription — null reads, no presumed flag, never
    pooled) and pool_fable, the same pooled read computed over weekly_fable."""

    def position(self):
        return {
            "accounts": [
                {
                    "provider": "Claude", "account": "work", "display": "Alpha",
                    "active": False, "plan": "Max 20x", "stale": False,
                    "observed_at": "2026-08-25T19:41:37Z",
                    "dormant": True, "dormant_since": "2026-09-11",
                    "limits": [{
                        "meter_id": "weekly_all", "label": "Weekly · all models",
                        "used_percent": None, "window_minutes": 10080,
                        "resets_at": "2026-09-13T02:59:59Z", "anchored": True,
                        "presumed": False, "rolled_periods": 0,
                    }],
                },
                {
                    "provider": "Claude", "account": "personal", "display": "Bravo",
                    "active": True, "plan": "Max 20x", "stale": False,
                    "observed_at": "2026-09-11T19:00:00Z",
                    "dormant": False, "dormant_since": None,
                    "limits": [{
                        "meter_id": "weekly_all", "label": "Weekly · all models",
                        "used_percent": 15.0, "window_minutes": 10080,
                        "resets_at": "2026-09-17T09:00:00Z", "anchored": True,
                        "presumed": False, "rolled_periods": 0,
                    }],
                },
            ],
            "openrouter": None,
            "switches": [],
            "warnings": [],
            "pool": {
                "provider": "Claude", "display": "Pooled", "label": "Weekly · all models",
                "meter_id": "weekly_all", "count": 1, "accounts": ["Bravo"],
                "used_percent": 15.0, "pace_percent": 20.0, "floor": False,
                "verdict": "trailing", "next_reset_at": "2026-09-17T09:00:00Z",
                "next_reset_account": "Bravo",
            },
            "pool_fable": {
                "provider": "Claude", "display": "Pooled", "label": "Weekly · Fable",
                "meter_id": "weekly_fable", "count": 1, "accounts": ["Bravo"],
                "used_percent": 22.0, "pace_percent": 20.0, "floor": False,
                "verdict": "ahead", "next_reset_at": "2026-09-17T09:00:00Z",
                "next_reset_account": "Bravo",
            },
        }

    def test_dormant_survives_into_the_snapshot(self):
        snapshot = deck_builder.build_snapshot(self.position())
        alpha = next(a for a in snapshot["accounts"] if a["display"] == "Alpha")
        bravo = next(a for a in snapshot["accounts"] if a["display"] == "Bravo")
        self.assertTrue(alpha["dormant"])
        self.assertEqual(alpha["dormant_since"], "2026-09-11")
        self.assertIsNone(alpha["limits"][0]["used_percent"])
        self.assertFalse(alpha["limits"][0]["presumed"])
        self.assertFalse(bravo["dormant"])
        self.assertIsNone(bravo["dormant_since"])

    def test_a_position_with_no_dormant_field_defaults_to_not_dormant(self):
        """Backward compatible with a snapshot built before the field existed."""
        pos = self.position()
        del pos["accounts"][1]["dormant"]
        del pos["accounts"][1]["dormant_since"]
        snapshot = deck_builder.build_snapshot(pos)
        bravo = next(a for a in snapshot["accounts"] if a["display"] == "Bravo")
        self.assertFalse(bravo["dormant"])
        self.assertIsNone(bravo["dormant_since"])

    def test_pool_fable_passes_through_beside_pool(self):
        snapshot = deck_builder.build_snapshot(self.position())
        self.assertEqual(snapshot["pool"]["meter_id"], "weekly_all")
        self.assertEqual(snapshot["pool_fable"]["meter_id"], "weekly_fable")
        self.assertEqual(snapshot["pool_fable"]["used_percent"], 22.0)

    def test_a_position_with_no_pool_fable_carries_none(self):
        pos = self.position()
        del pos["pool_fable"]
        snapshot = deck_builder.build_snapshot(pos)
        self.assertIsNone(snapshot["pool_fable"])

    def test_dormant_never_lets_an_identity_leak_through_the_blob(self):
        snapshot = deck_builder.build_snapshot(self.position())
        blob = deck_builder.snapshot_blob(snapshot)
        self.assertIn('"dormant":true', blob)
        self.assertIn('"dormant_since":"2026-09-11"', blob)


class DormantSidecarTests(unittest.TestCase):
    """The one fact history-src/build.py borrows to stop a dormant account's
    trail: a small {display: dormant_since} file, rewritten on every deck
    build — never accumulated, so a dormancy that ends disappears from it."""

    def test_writes_only_the_dormant_accounts(self):
        accounts = [
            {"display": "Alpha", "dormant": True, "dormant_since": "2026-09-11"},
            {"display": "Bravo", "dormant": False, "dormant_since": None},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dormant.json"
            deck_builder.write_dormant_sidecar(accounts, path)
            written = json.loads(path.read_text())
        self.assertEqual(written, {"Alpha": "2026-09-11"})

    def test_an_empty_position_writes_an_empty_map_not_a_stale_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dormant.json"
            path.write_text(json.dumps({"Alpha": "2026-08-01"}))
            deck_builder.write_dormant_sidecar(
                [{"display": "Alpha", "dormant": False, "dormant_since": None}], path)
            written = json.loads(path.read_text())
        self.assertEqual(written, {})

    def test_a_missing_directory_does_not_fail_the_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "dormant.json"
            deck_builder.write_dormant_sidecar(
                [{"display": "Alpha", "dormant": True, "dormant_since": "2026-09-11"}], path)
            self.assertEqual(json.loads(path.read_text()), {"Alpha": "2026-09-11"})


if __name__ == "__main__":
    unittest.main()


class ScriptSafetyTests(unittest.TestCase):
    """The snapshot lands inside <script>: nothing in it may close the tag."""

    def test_provider_text_cannot_break_out_of_the_script_tag(self):
        blob = deck_builder.snapshot_blob({"warnings": ["</script><script>alert(1)</script>", "a & b"]})
        self.assertNotIn("</script>", blob)
        self.assertNotIn("<", blob)
        self.assertNotIn("&", blob)
        self.assertEqual(json.loads(blob)["warnings"][0], "</script><script>alert(1)</script>")
