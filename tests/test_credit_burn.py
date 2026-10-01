"""Usage credits: a window delta of the month-to-date counter.

The percent and the ◆ stay. The 💸 suffix is the rise during that window, and
only when the rise is above zero. The 5-hour figure is inside the 7-day figure.
Fable is not marked.
"""

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import glideslope
import sampler

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
WEEK_RESET = dt.datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
SESSION_RESET = dt.datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
WEEK_START = WEEK_RESET - dt.timedelta(minutes=7 * 24 * 60)
SESSION_START = SESSION_RESET - dt.timedelta(minutes=300)


def iso(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def store(rows: list[tuple[dt.datetime, int]]) -> Path:
    """A throwaway sample store of (observed, credit_minor) for Claude/work."""
    path = Path(tempfile.mkdtemp(prefix="glideslope-credit-")) / "samples.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE samples (ts TEXT, provider TEXT, account TEXT, display TEXT,"
        " meter TEXT, used_percent REAL, spend_usd REAL, window_minutes INTEGER,"
        " resets_at TEXT, observed_at TEXT, active INTEGER, held_by TEXT,"
        " credit_minor INTEGER)"
    )
    for observed, minor in rows:
        con.execute(
            "INSERT INTO samples VALUES (?, 'Claude', 'work', 'Alpha', 'extra_usage',"
            " NULL, NULL, NULL, NULL, ?, 1, NULL, ?)",
            (iso(observed), iso(observed), minor),
        )
    con.commit()
    con.close()
    return path


def account(used_minor: int = 11820, **extra) -> dict:
    row = {
        "provider": "Claude",
        "account": "work",
        "display": "Alpha",
        "plan": "Max 20x",
        "observed_at": iso(NOW),
        "extra_usage": {
            "enabled": True, "used_minor": used_minor, "currency": "USD", "exponent": 2,
        },
        "limits": [
            {"meter_id": "weekly_all", "label": "Weekly · all models", "used_percent": 100.0,
             "window_minutes": 7 * 24 * 60, "resets_at": WEEK_RESET},
            {"meter_id": "weekly_fable", "label": "Weekly · Fable", "used_percent": 100.0,
             "window_minutes": 7 * 24 * 60, "resets_at": WEEK_RESET},
            {"meter_id": "session", "label": "5h session", "used_percent": 100.0,
             "window_minutes": 300, "resets_at": SESSION_RESET},
        ],
    }
    row.update(extra)
    return row


def mark(row: dict, db: Path, now: dt.datetime = NOW) -> dict:
    glideslope.mark_credit_burn([row], now, db=db)
    return row


class DeltaTests(unittest.TestCase):
    def test_five_hour_spend_is_inside_the_week_and_fable_is_unmarked(self):
        # 10000 at the week boundary, 11640 just before this session, 11820 now.
        db = store([
            (WEEK_START - dt.timedelta(minutes=5), 10000),
            (SESSION_START - dt.timedelta(minutes=5), 11640),
        ])
        row = mark(account(11820), db)
        weekly, fable, session = row["limits"]
        self.assertEqual(weekly["extra_amount"], "18.20")
        self.assertEqual(weekly["extra_currency"], "USD")
        self.assertFalse(weekly["extra_floor"])
        self.assertEqual(session["extra_amount"], "1.80")
        self.assertFalse(session["extra_floor"])
        self.assertNotIn("extra_amount", fable)

    def test_the_reading_in_hand_counts_before_it_is_stored(self):
        db = store([(WEEK_START - dt.timedelta(minutes=5), 10000)])
        row = mark(account(11820), db)
        self.assertEqual(row["limits"][0]["extra_amount"], "18.20")

    def test_one_sample_waits(self):
        db = store([])
        row = mark(account(11820), db)
        self.assertNotIn("extra_amount", row["limits"][0])
        self.assertNotIn("extra_amount", row["limits"][2])

    def test_an_in_window_baseline_is_a_floor(self):
        db = store([
            (WEEK_START + dt.timedelta(hours=1), 10000),
            (WEEK_START + dt.timedelta(hours=2), 10180),
        ])
        row = mark(account(10180), db)
        weekly = row["limits"][0]
        self.assertEqual(weekly["extra_amount"], "1.80")
        self.assertTrue(weekly["extra_floor"])

    def test_a_far_prior_does_not_import_the_previous_window(self):
        db = store([(WEEK_START - dt.timedelta(days=2), 1000)])
        row = mark(account(5000), db)
        self.assertNotIn("extra_amount", row["limits"][0])

    def test_a_counter_drop_keeps_the_spend_before_it_and_is_a_floor(self):
        start = WEEK_START
        db = store([
            (start - dt.timedelta(minutes=5), 5000),
            (start + dt.timedelta(hours=1), 5200),
            (start + dt.timedelta(hours=2), 100),
        ])
        row = mark(account(180), db)
        weekly = row["limits"][0]
        # 2.00 before the restart, 1.00 at the first read after it, 0.80 since
        self.assertEqual(weekly["extra_amount"], "3.80")
        self.assertTrue(weekly["extra_floor"])

    def test_a_month_boundary_climbed_past_counts_only_the_rise(self):
        before = dt.datetime(2026, 9, 30, 23, 56, tzinfo=UTC)
        after = dt.datetime(2026, 10, 1, 0, 2, tzinfo=UTC)
        found = glideslope.credit_window_delta(
            [(before - dt.timedelta(hours=1), 100), (before, 100), (after, 500)],
            before - dt.timedelta(hours=1))
        self.assertEqual(found, (400, True))

    def test_a_stale_account_marks_its_dollars_as_a_floor(self):
        db = store([(WEEK_START - dt.timedelta(minutes=5), 10000)])
        row = mark(account(11820, stale=True), db)
        self.assertEqual(row["limits"][0]["extra_amount"], "18.20")
        self.assertTrue(row["limits"][0]["extra_floor"])

    def test_a_quiet_counter_sets_nothing(self):
        db = store([(WEEK_START - dt.timedelta(minutes=5), 10000)])
        row = mark(account(10000), db)
        self.assertNotIn("extra_amount", row["limits"][0])

    def test_a_new_window_does_not_keep_the_previous_windows_dollars(self):
        # The old window's samples sit days before the new start. Same counter,
        # no rise since the reset: the cell is clear.
        new_start = NOW - dt.timedelta(hours=1)
        reset = new_start + dt.timedelta(days=7)
        db = store([
            (new_start - dt.timedelta(days=2), 5000),
            (new_start - dt.timedelta(days=1), 5000),
        ])
        row = account(5000)
        row["limits"][0]["resets_at"] = reset
        mark(row, db)
        self.assertNotIn("extra_amount", row["limits"][0])

    def test_an_early_reset_measures_from_the_rebase(self):
        rebased = WEEK_START + dt.timedelta(days=3)
        db = store([
            (WEEK_START - dt.timedelta(minutes=5), 1000),
            (rebased - dt.timedelta(minutes=5), 4000),
        ])
        row = account(4500)
        row["limits"][0]["rebased_at"] = rebased
        mark(row, db)
        self.assertEqual(row["limits"][0]["extra_amount"], "5.00")
        self.assertFalse(row["limits"][0]["extra_floor"])

    def test_a_missing_counter_and_a_missing_column_leave_the_cell_alone(self):
        db = store([(WEEK_START - dt.timedelta(minutes=5), 1000)])
        row = account()
        row.pop("extra_usage")
        mark(row, db)
        self.assertNotIn("extra_amount", row["limits"][0])

        bare = Path(tempfile.mkdtemp(prefix="glideslope-credit-")) / "samples.db"
        con = sqlite3.connect(bare)
        con.execute("CREATE TABLE samples (ts TEXT, provider TEXT, account TEXT, meter TEXT,"
                    " used_percent REAL, observed_at TEXT)")
        con.commit()
        con.close()
        row = account()
        glideslope.mark_credit_burn([row], NOW, db=bare)
        self.assertNotIn("extra_amount", row["limits"][0])


class CellTests(unittest.TestCase):
    def limit(self, **extra):
        row = {
            "meter_id": "weekly_all", "label": "Weekly · all models",
            "used_percent": 100.0, "window_minutes": 7 * 24 * 60, "resets_at": WEEK_RESET,
        }
        row.update(extra)
        return row

    def test_the_suffix_keeps_the_percent_and_the_mark(self):
        cell = glideslope.used_cell(
            {"display": "Alpha"}, self.limit(extra_amount="18.20", extra_currency="USD"),
            NOW, color=False)
        self.assertTrue(cell.startswith("100%"))
        self.assertIn("(◆ ", cell)
        self.assertIn("  💸 $18.20", cell)
        self.assertNotIn("floor", cell)

    def test_floor_is_a_word_after_the_amount(self):
        cell = glideslope.used_cell(
            {"display": "Alpha"},
            self.limit(extra_amount="1.80", extra_currency="USD", extra_floor=True),
            NOW, color=False)
        self.assertIn("  💸 $1.80 floor", cell)

    def test_euro_and_an_unmarked_currency(self):
        euro = glideslope.used_cell(
            {"display": "Alpha"}, self.limit(extra_amount="1.80", extra_currency="EUR"),
            NOW, color=False)
        self.assertIn("  💸 €1.80", euro)
        other = glideslope.used_cell(
            {"display": "Alpha"}, self.limit(extra_amount="1.80", extra_currency="BRL"),
            NOW, color=False)
        self.assertIn("  💸 1.80 BRL", other)

    def test_a_quiet_cell_and_an_unread_cell_grow_no_suffix(self):
        quiet = glideslope.used_cell({"display": "Alpha"}, self.limit(), NOW, color=False)
        self.assertNotIn("💸", quiet)
        self.assertIn("100%", quiet)
        unread = glideslope.used_cell(
            {"display": "Alpha"},
            self.limit(used_percent=None, extra_amount="9.00", extra_currency="USD"),
            NOW, color=False)
        self.assertEqual(unread, "unread")

    def test_a_stale_account_keeps_the_dollars_off_the_pace(self):
        cell = glideslope.used_cell(
            {"display": "Alpha", "stale": True},
            self.limit(extra_amount="18.20", extra_currency="USD"),
            NOW, color=False)
        self.assertEqual(cell, "100%  💸 $18.20")

    def test_the_weekly_column_carries_it_and_fable_does_not(self):
        db = store([
            (WEEK_START - dt.timedelta(minutes=5), 10000),
            (SESSION_START - dt.timedelta(minutes=5), 11640),
        ])
        row = mark(account(11820), db)
        weekly = glideslope.render_weekly_summary([row], None, NOW, color=False)
        line = next(item for item in weekly.splitlines() if "Alpha" in item)
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        self.assertIn("💸 $18.20", cells[1])
        self.assertNotIn("💸", cells[2])
        self.assertIn("100%", cells[2])
        rendered = glideslope.render_markdown([row], NOW, [], color=False)
        session = next(item for item in rendered.splitlines() if "5h session" in item)
        fable = next(item for item in rendered.splitlines() if "Fable" in item)
        self.assertIn("💸 $1.80", session)
        self.assertIn("100%", session)
        self.assertNotIn("💸", fable)


class CarryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = glideslope.PROVIDER_CACHE_DIR
        glideslope.PROVIDER_CACHE_DIR = Path(self.tmp.name) / "provider-cache"
        self.addCleanup(setattr, glideslope, "PROVIDER_CACHE_DIR", original)

    def raw(self, when: dt.datetime, *, extra: int | None, percent: float = 10, stale: bool = False) -> dict:
        row = {
            "email": "a@example.com",
            "active": not stale,
            "stale": stale,
            "fetched_at": iso(when),
            "limits": [{
                "label": "session (5h)", "percent": percent,
                "resets_at": "2026-09-30T18:00:00Z",
            }],
        }
        if extra is not None:
            row["extra_usage"] = {
                "enabled": True, "used_minor": extra, "currency": "USD", "exponent": 2,
            }
        return {"work": row}

    def test_a_live_read_journals_the_counter_and_a_lockout_serves_it(self):
        earlier, later = NOW - dt.timedelta(hours=2), NOW
        glideslope.keep_claude_state(
            glideslope.normalize_claude(self.raw(later, extra=500), later),
            self.raw(later, extra=500), later)
        kept = glideslope.cached_provider_read("claude-work", later)
        self.assertEqual(kept[0]["extra_usage"]["used_minor"], 500)

        locked = glideslope.keep_claude_state(
            glideslope.normalize_claude(self.raw(earlier, extra=None, stale=True), earlier),
            self.raw(earlier, extra=None, stale=True), later)
        self.assertEqual(locked[0]["extra_usage"]["used_minor"], 500)

    def test_a_journal_without_a_counter_does_not_keep_an_older_one(self):
        earlier, later = NOW - dt.timedelta(hours=2), NOW
        glideslope.keep_claude_state(
            glideslope.normalize_claude(self.raw(later, extra=None), later),
            self.raw(later, extra=None), later)
        locked = glideslope.keep_claude_state(
            glideslope.normalize_claude(self.raw(earlier, extra=500, stale=True), earlier),
            self.raw(earlier, extra=500, stale=True), later)
        self.assertNotIn("extra_usage", locked[0])

    def test_a_meter_beacon_drops_the_counter_and_a_login_beacon_carries_it(self):
        snapshot = self.raw(NOW - dt.timedelta(hours=5), extra=500, stale=True)
        meter = {
            "name": "studio", "age_seconds": 5, "observed_at": NOW,
            "snapshot": {"remote": {
                "email": "a@example.com", "logged_in": True, "source": "meter-token",
                "limits": [{
                    "label": "session (5h)", "percent": 12,
                    "resets_at": "2026-09-30T18:00:00Z",
                }],
            }},
        }
        glideslope.merge_satellite_claude(snapshot, [meter], NOW)
        self.assertNotIn("extra_usage", snapshot["work"])
        self.assertEqual(snapshot["work"]["limits"][0]["percent"], 12)

        fresh = self.raw(NOW, extra=500, stale=False)
        glideslope.merge_satellite_claude(fresh, [meter], NOW - dt.timedelta(minutes=1))
        self.assertEqual(fresh["work"]["extra_usage"]["used_minor"], 500)

        stale = self.raw(NOW - dt.timedelta(hours=5), extra=None, stale=True)
        login = {
            "name": "studio", "age_seconds": 5, "observed_at": NOW,
            "snapshot": {"remote": {
                "email": "a@example.com", "logged_in": True,
                "limits": [{
                    "label": "session (5h)", "percent": 20,
                    "resets_at": "2026-09-30T18:00:00Z",
                }],
                "extra_usage": {
                    "enabled": True, "used_minor": 900, "currency": "eur", "exponent": 2,
                },
            }},
        }
        glideslope.merge_satellite_claude(stale, [login], NOW)
        self.assertEqual(stale["work"]["extra_usage"]["used_minor"], 900)
        self.assertEqual(stale["work"]["extra_usage"]["currency"], "EUR")


class SamplerTests(unittest.TestCase):
    def position(self, extra):
        account = {
            "provider": "Claude", "account": "work", "display": "Alpha",
            "observed_at": iso(NOW), "active": True, "logins": ["laptop"],
            "limits": [{
                "meter_id": "session", "used_percent": 10, "window_minutes": 300,
                "resets_at": iso(SESSION_RESET),
            }],
        }
        if extra is not None:
            account["extra_usage"] = extra
        return {"accounts": [account], "openrouter": {"weekly_usd": 3.5, "display": "OpenRouter"}}

    def test_a_present_counter_is_journaled_including_zero(self):
        rows = sampler.rows_from(self.position({
            "enabled": False, "used_minor": 0, "currency": "USD", "exponent": 2,
        }), iso(NOW))
        extra = [row for row in rows if row[4] == "extra_usage"]
        limits = [row for row in rows if row[4] == "session"]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0][12], 0)
        self.assertIsNone(extra[0][5])  # used_percent
        self.assertIsNone(extra[0][6])  # spend_usd, left to OpenRouter
        self.assertIsNone(limits[0][12])
        spend = [row for row in rows if row[4] == "spend"]
        self.assertEqual(spend[0][6], 3.5)
        self.assertIsNone(spend[0][12])
        self.assertIsNone(spend[0][13])
        self.assertIsNone(limits[0][13])

    def test_the_plan_is_journaled_on_meter_and_credit_rows_only(self):
        position = self.position({
            "enabled": True, "used_minor": 12, "currency": "USD", "exponent": 2,
        })
        position["accounts"][0]["plan"] = "SuperGrok Plus"
        rows = sampler.rows_from(position, iso(NOW))
        session = [row for row in rows if row[4] == "session"]
        extra = [row for row in rows if row[4] == "extra_usage"]
        spend = [row for row in rows if row[4] == "spend"]
        self.assertEqual(session[0][13], "SuperGrok Plus")
        self.assertEqual(extra[0][13], "SuperGrok Plus")
        self.assertIsNone(spend[0][13])
        blank = self.position(None)
        blank["accounts"][0]["plan"] = ""
        self.assertIsNone(sampler.rows_from(blank, iso(NOW))[0][13])

    def test_an_absent_block_journals_nothing(self):
        rows = sampler.rows_from(self.position(None), iso(NOW))
        self.assertFalse(any(row[4] == "extra_usage" for row in rows))

    def test_migrate_adds_the_column_once(self):
        path = Path(tempfile.mkdtemp(prefix="glideslope-credit-")) / "samples.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE samples (ts TEXT, held_by TEXT)")
        sampler.migrate(con)
        sampler.migrate(con)
        columns = [row[1] for row in con.execute("PRAGMA table_info(samples)")]
        self.assertIn("credit_minor", columns)
        self.assertEqual(columns.count("plan"), 1)
        con.close()


class TemplateTests(unittest.TestCase):
    def test_the_mark_is_on_the_indicators_and_off_the_compact_register(self):
        root = Path(__file__).parents[1] / "views"
        deck = (root / "deck-src" / "deck.tmpl.html").read_text()
        popup = (root / "popup-src" / "popup.tmpl.html").read_text()
        self.assertIn("function extraMark", deck)
        self.assertIn("extraMark(w.extraAmount, w.extraCurrency, w.extraFloor)", deck)
        self.assertIn("extraMark(window.extra_amount, window.extra_currency, window.extra_floor)", popup)
        register = popup.split("const used = entry.window.resets_at", 1)[1].split(
            "if (entry.account.provider", 1)[0]
        self.assertIn("reg-used", register)
        self.assertNotIn("extraMark", register)
        pool = deck.split("const poolCell = ", 1)[1].split("const fablePool", 1)[0]
        self.assertNotIn("extraMark", pool)


if __name__ == "__main__":
    unittest.main()
