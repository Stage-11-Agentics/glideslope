"""Reset banks: Claude's `cedar_ember` grants and Codex's reset credits, read and shown as one thing."""

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import claude_account  # noqa: E402
import glideslope  # noqa: E402


@pytest.fixture(autouse=True)
def call_signs(monkeypatch):
    """Name the fixture accounts without touching the module's learned call signs."""
    monkeypatch.setattr(glideslope, "CLAUDE_CALL_SIGNS", {"alpha": "Alpha", "bravo": "Bravo", "charlie": "Charlie"})

NOW = dt.datetime(2026, 11, 9, 21, 0, tzinfo=dt.timezone.utc)


def grant(gid, label, left=1):
    return {"id": gid, "label": label, "resets_total": 1, "resets_left": left,
            "starts_at": "2026-11-09T18:00:00+00:00", "ends_at": "2027-03-09T07:59:00+00:00",
            "clears": ["seven_day_overage_included"], "paused": False, "usable_now": True,
            "use_requires_limit": False, "type": "full"}


# The shape the usage endpoint returned for a 365+ day Max account, 2026-11-09.
USAGE = {"limits": [], "cedar_ember": {
    "eligible": True, "ineligible_reason": None, "at_limit": False, "exhausted": [],
    "grants": [
        grant("launch-2610-fable", "New model launch: one Fable reset for Max, Team Premium and Enterprise Premium"),
        grant("launch-2610-fable-12mo", "New model launch: one more Fable reset at 12 months' tenure"),
        grant("launch-2610-fable-6mo", "New model launch: one more Fable reset at 6 months' tenure"),
    ],
    "next_grant_id": "launch-2610-fable", "weekly_resets_at": "2026-11-15T03:00:00+00:00",
    "cooldown_until": None, "event_props": {"tenure_bucket": "365+"}}}


def claude(alias, bank, stale=False):
    raw = {"email": f"{alias}@example.com", "stale": stale, "limits": [
        {"label": "weekly (all models)", "percent": 40.0, "resets_at": "2026-11-12T09:00:00+00:00"}]}
    if bank is not None:
        raw["reset_bank"] = bank
    return raw


def test_a_bank_is_parsed_from_the_usage_read():
    bank = claude_account.parse_reset_bank(USAGE, read_at="2026-11-09T20:55:00Z")
    assert [g["left"] for g in bank["grants"]] == [1, 1, 1]
    assert bank["tenure"] == "365+" and bank["next_grant_id"] == "launch-2610-fable"


def test_a_refused_client_identity_is_unread_not_empty():
    refused = {"cedar_ember": {"eligible": False, "ineligible_reason": "cli_version", "grants": []}}
    assert claude_account.parse_reset_bank(refused) is None
    assert claude_account.parse_reset_bank({"limits": []}) is None
    plan = {"cedar_ember": {"eligible": False, "ineligible_reason": "plan", "grants": []}}
    assert claude_account.parse_reset_bank(plan)["grants"] == []  # a real empty bank


def test_status_cells_count_mark_and_pool():
    bank = claude_account.parse_reset_bank(USAGE, read_at="2026-11-09T20:55:00Z")
    old = claude_account.parse_reset_bank(USAGE, read_at="2026-11-08T20:55:00Z")
    accounts = glideslope.normalize_claude(
        {"alpha": claude("alpha", bank), "bravo": claude("bravo", None), "charlie": claude("charlie", old)}, NOW)
    by = {a["account"]: a for a in accounts}
    assert glideslope.resets_cell(by["alpha"], color=False) == "3"
    assert glideslope.resets_cell(by["bravo"], color=False) == "unread"
    assert glideslope.resets_cell(by["charlie"], color=False) == "3 stale"  # a ceiling: one may be spent
    assert glideslope.pooled_resets_cell(accounts, color=False) == "6 floor"  # Bravo unread


def test_one_table_for_both_providers_names_the_unread():
    bank = claude_account.parse_reset_bank(USAGE, read_at="2026-11-09T20:55:00Z")
    accounts = glideslope.normalize_claude({"alpha": claude("alpha", bank), "bravo": claude("bravo", None)}, NOW)
    raw = {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1795000000}},
           "rateLimitResetCredits": {"availableCount": 1, "credits": [
               {"status": "available", "grantedAt": 1793000000, "expiresAt": 1795592000}]}}
    accounts += glideslope.codex_accounts(glideslope.normalize_codex(raw, NOW))
    rendered = glideslope.render_reset_banks(accounts, NOW)
    assert "| Claude · Alpha | One Fable reset · used next | 1 | Weekly · Fable |" in rendered
    assert "One more Fable reset at 6 months' tenure" in rendered
    assert "| Codex | Full reset | 1 | Weekly · all models |" in rendered
    assert "unread: Bravo." in rendered


def test_an_expired_grant_is_not_banked():
    bank = claude_account.parse_reset_bank(USAGE, read_at="2026-11-09T20:55:00Z")
    later = dt.datetime(2027, 3, 10, tzinfo=dt.timezone.utc)
    assert glideslope.claude_banked_resets(bank, later)["count"] == 0
