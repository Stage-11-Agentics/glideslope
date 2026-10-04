"""Hermetic tests for the public API-equivalent Spend view builder."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "views" / "spend-src" / "build.py"
TEMPLATE = ROOT / "views" / "spend-src" / "spend.tmpl.html"
_spec = importlib.util.spec_from_file_location("spend_page_build", BUILDER)
build = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(build)

NOW = dt.datetime(2026, 9, 24, 12, tzinfo=dt.timezone.utc)
NOW_TEXT = "2026-09-24T12:00:00Z"
SAMPLE_SCHEMA = """CREATE TABLE samples (
  ts TEXT, provider TEXT, account TEXT, display TEXT, meter TEXT,
  used_percent REAL, spend_usd REAL, window_minutes INTEGER, resets_at TEXT,
  observed_at TEXT, active INTEGER
)"""


def bucket(usd: float, **fields: float) -> dict:
    return {
        "usd": usd, "tokens": 1000, "input": 600, "output": 250,
        "cache_read": 100, "cache_write": 50, "reasoning": 10,
        "requests": 5, "premium_usd": 0.75,
        "speed": {
            "standard": {"usd": usd * 0.6, "tokens": 600, "requests": 2, "premium_usd": 0.0},
            "flex": {"usd": usd * 0.1, "tokens": 100, "requests": 1, "premium_usd": -0.25},
            "fast": {"usd": usd * 0.2, "tokens": 200, "requests": 1, "premium_usd": 1.0},
            "unknown": {"usd": usd * 0.1, "tokens": 100, "requests": 1, "premium_usd": 0.0},
            "synthetic-tier": {"usd": 999.0, "tokens": 999, "requests": 99, "premium_usd": 999.0},
        },
        **fields,
    }


def spend_payload() -> dict:
    days = [(dt.date(2026, 9, 24) - dt.timedelta(days=offset)).isoformat()
            for offset in range(59, -1, -1)]
    return {
        "generated_at": "2026-09-24T11:45:00Z",
        "collected_at": {"synthetic-machine": "2026-09-24T11:44:00Z"},
        "first_request_at": "2025-09-17T17:02:22Z",
        "totals": {
            horizon: bucket(value, input=600, cache_read=100, cache_write=50, output=250,
                            fable_usd=1.25, fable_tokens=125)
            for horizon, value in (("d1", 11.11), ("d7", 222.22), ("d30", 3333.33),
                                   ("d90", 5000.0), ("all", 44444.44))
        },
        "providers": {
            "Total": {horizon: bucket(44444.44) for horizon in ("d1", "d7", "d30", "d90", "all")},
            "Anthropic": {horizon: bucket(40000.04) for horizon in ("d1", "d7", "d30", "d90", "all")},
            "Codex": {horizon: bucket(4000.0) for horizon in ("d1", "d7", "d30", "d90", "all")},
            "Grok": {horizon: bucket(444.4) for horizon in ("d1", "d7", "d30", "d90", "all")},
            "synthetic-private-provider": {"all": bucket(99.0)},
        },
        "accounts": [
            {
                "name": "Alpha", "provider": "Claude", "plan_tier": "Max 5x",
                "all": bucket(12345.67), "d1": bucket(10.0), "d7": bucket(70.0),
                "d30": bucket(300.0),
                "models": {"fable": bucket(123.45), "opus": bucket(456.78), "ignored model": bucket(1.0)},
                "window": {"end": "2026-09-27T03:00:00Z", "usd_per_pct_all": 0.75,
                           "private_extra": "must be removed"},
            },
            {"name": "Codex", "provider": "Codex", "plan_tier": "Example Plan",
             **{horizon: bucket(value) for horizon, value in (("d1", 10.0), ("d7", 70.0),
                                                                 ("d30", 300.0), ("d90", 600.0),
                                                                 ("all", 4000.0))}},
            {"name": "Grok", "provider": "Grok", "plan_tier": "Example Plan",
             **{horizon: bucket(value) for horizon, value in (("d1", 2.0), ("d7", 14.0),
                                                                 ("d30", 60.0), ("d90", 120.0),
                                                                 ("all", 444.4))}},
            {"name": "unattributed", "provider": "Claude", "plan_tier": "Example Plan",
             **{horizon: bucket(value) for horizon, value in (("d1", 1.0), ("d7", 7.0),
                                                                 ("d30", 30.0), ("d90", 60.0),
                                                                 ("all", 100.0))}},
            {"name": "builder@example.test", "provider": "Claude", "all": bucket(9.0)},
            {"name": "/tmp/synthetic-path", "provider": "Claude", "all": bucket(8.0)},
            {"name": "Unsupported Account", "provider": "SyntheticProvider", "all": bucket(7.0)},
        ],
        "leverage": {
            "claude_multiple": 22.2,
            "claude_api_equivalent_d30_usd": 3333.33,
            "claude_subscriptions_monthly_usd": 150.0,
            "codex_multiple": 5.0,
            "codex_api_equivalent_d30_usd": 750.0,
            "codex_subscription_monthly_usd": 200.0,
            "private_client_monthly_usd": 9999.0,
        },
        "pricing": {
            "litellm_age_h": 2.0,
            "unpriced_tokens": {"model-family-1": 15, "synthetic machine name": 99},
            "underpriced_tokens": {"model-family-2": 8},
            "derived_tokens": {"model-family-3": 4},
            "estimated_tokens": {"model-family-4": 2},
            "speed_evidence": {"codex/fast/settings": 4},
            "fast_mode_armed": {"claude": True, "codex": False},
            "private_extra": "must be removed",
        },
        "meters": {
            "Alpha/weekly_all": {"usd": 20.0, "tokens": 2000, "fable_only": False,
                                        "start": "2026-09-20T03:00:00Z", "end": "2026-09-27T03:00:00Z"},
            "Alpha/weekly_fable": {"usd": 2.0, "tokens": 200,
                                          "start": "2026-09-20T03:00:00Z", "end": "2026-09-27T03:00:00Z",
                                          "fable_only": True},
            "Codex/codex": {"usd": 5.0, "tokens": 500,
                            "start": "2026-09-20T03:00:00Z", "end": "2026-09-27T03:00:00Z",
                            "fable_only": False},
            "Alpha/synthetic-machine": {"usd": 100.0},
            "synthetic-machine/weekly_all": {"usd": 100.0},
        },
        "machines": {"synthetic-machine": {"account": "Alpha"}},
        "attribution": {"private-extra": {"account": "Alpha"}},
        "daily": {
            "days": days,
            "series": {
                "Alpha": [round((index + 1) * 0.07, 2) for index in range(60)],
                "Alpha:fable": [round((index + 1) * 0.015, 2) for index in range(60)],
                "Codex": [round((index + 1) * 0.03, 2) for index in range(60)],
                "synthetic-provider": [99.0] * 60,
            },
        },
        "model_tokens": [{"provider": "Claude", "model": "model-family-1", "priced": True,
                          "d1": bucket(1.0), "d7": bucket(2.0), "d30": bucket(3.0), "all": bucket(4.0)}],
        "private_extra": "must be removed",
    }


def make_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(SAMPLE_SCHEMA)
    con.executemany(
        "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-09-24T11:58:00Z", "Claude", "work", "Alpha", "weekly_all",
             42.0, None, 10080, "2026-09-27T03:00:00Z", "2026-09-24T11:58:00Z", 1),
            ("2026-09-24T11:57:00Z", "Codex", "codex", "Codex", "codex",
             15.0, None, 10080, "2026-09-27T03:00:00Z", "2026-09-24T11:57:00Z", 1),
        ],
    )
    con.commit()
    con.close()
    return path


def injected(page: str, script_id: str) -> dict:
    match = re.search(rf'<script id="{script_id}" type="application/json">(.*?)</script>', page, re.S)
    assert match, f"missing {script_id}"
    return json.loads(match.group(1))


def test_builder_selects_public_fields_and_keeps_missing_values_unknown(tmp_path):
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(spend_payload()))
    db = make_db(tmp_path / "samples.db")
    output = tmp_path / "spend.html"

    message = build.build(source, db, output, TEMPLATE, NOW)
    page = output.read_text()
    data = injected(page, "spend-data")
    meters = injected(page, "meter-data")
    metadata = injected(page, "build-data")

    assert "4 accounts" in message and "2 meter readings" in message
    assert not any(token in page for token in build.TOKENS)
    assert set(account["name"] for account in data["accounts"]) == {"Alpha", "Codex", "Grok", "unattributed"}
    assert set(data["providers"]) == {"Total", "Anthropic", "Codex", "Grok"}
    assert "machines" not in data and "attribution" not in data and "private_extra" not in data
    assert "private_client_monthly_usd" not in data["leverage"]
    assert "private_extra" not in data["pricing"]
    assert data["pricing"]["fast_mode_armed"] == {"claude": True, "codex": False}
    assert {"flex", "unknown"} <= set(data["totals"]["all"]["speed"])
    assert "synthetic-tier" not in data["totals"]["all"]["speed"]
    assert "synthetic machine name" not in data["pricing"]["unpriced_tokens"]
    assert "ignored model" not in data["accounts"][0]["models"]
    assert "Alpha" in data["daily"]["series"]
    assert "synthetic-provider" not in data["daily"]["series"]
    assert "Flex" in page and "Price adjustment" in page
    assert "synthetic-machine" not in page and "work" not in page and "store-two" not in page
    assert meters["Alpha"]["here"] is True
    assert meters["Codex"]["here"] is False
    assert meters["Codex"]["meters"]["codex"]["pct"] == 15.0
    assert meters["Codex"]["meters"]["codex"]["minutes"] == 10080
    assert data["meters"]["Codex/codex"]["usd"] == 5.0
    assert "Codex" in page and "Grok" in page
    assert meters["Alpha"]["meters"]["weekly_all"]["pct"] == 42.0
    assert metadata["meters_observed_at"] == "2026-09-24T11:58:00Z"
    assert (output.stat().st_mode & 0o777) == 0o600


def test_builder_escapes_script_closers_and_preserves_a_last_good_page(tmp_path):
    payload = spend_payload()
    payload["accounts"][0]["plan_tier"] = "</script><script>alert(1)</script>"
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(payload))
    output = tmp_path / "spend.html"
    db = make_db(tmp_path / "samples.db")
    build.build(source, db, output, TEMPLATE, NOW)
    page = output.read_text()
    assert "</script><script>alert(1)</script>" not in page
    assert "\\u003c/script\\u003e" in page

    output.write_text("last good page")
    for invalid in ("{bad json", "[]", json.dumps({"generated_at": NOW_TEXT, "accounts": []})):
        source.write_text(invalid)
        with pytest.raises(build.BuildError):
            build.build(source, db, output, TEMPLATE, NOW)
        assert output.read_text() == "last good page"


def test_missing_sample_database_builds_with_unknown_meter_cells(tmp_path):
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(spend_payload()))
    output = tmp_path / "spend.html"
    message = build.build(source, tmp_path / "absent.db", output, TEMPLATE, NOW)
    assert "0 meter readings" in message
    assert injected(output.read_text(), "meter-data") == {}


def test_nav_links_are_relative_to_the_output_and_target_the_public_views_directory(tmp_path):
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(spend_payload()))
    db = make_db(tmp_path / "samples.db")
    public_views = tmp_path / "glideslope" / "views"
    private_views = tmp_path / "sidecar-root" / "views"
    public_views.mkdir(parents=True)
    private_views.mkdir(parents=True)
    (public_views / "deck.html").write_text("detail")
    (public_views / "history.html").write_text("history")
    output = private_views / "spend.html"

    build.build(source, db, output, TEMPLATE, NOW, views_dir=public_views)

    metadata = injected(output.read_text(), "build-data")
    assert metadata["links"] == {
        "detail": "../../glideslope/views/deck.html",
        "history": "../../glideslope/views/history.html",
    }
    assert not any(value.startswith(("file:", "/")) for value in metadata["links"].values())

    (public_views / "history.html").unlink()
    build.build(source, db, output, TEMPLATE, NOW, views_dir=public_views)
    page = output.read_text()
    assert injected(page, "build-data")["links"]["history"] is None
    assert str(tmp_path) not in page


def test_private_extension_payload_and_template_hook_stay_outside_public_spend_data(tmp_path):
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(spend_payload()))
    output = tmp_path / "spend.html"
    private = {
        "machines": {"private-machine": {"all": {"usd": 123.45}}},
        "collected_at": {"private-machine": "2026-09-24T11:44:00Z"},
        "held_by": {"Alpha": ["private-machine"]},
    }
    hook = '<aside id="private-extension-hook">private only</aside>'

    build.build(source, tmp_path / "absent.db", output, TEMPLATE, NOW,
                extension_data=private, extension_hook=hook)

    page = output.read_text()
    assert "private only" in page
    assert injected(page, "spend-extension-data") == private
    public_spend = injected(page, "spend-data")
    assert "machines" not in public_spend and "collected_at" not in public_spend
    assert "private-machine" not in json.dumps(public_spend)


def test_missing_daily_series_stays_unknown_for_codex_history(tmp_path):
    payload = spend_payload()
    payload["daily"]["series"].pop("Codex")
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(payload))
    db = make_db(tmp_path / "samples.db")
    output = tmp_path / "spend.html"

    build.build(source, db, output, TEMPLATE, NOW)

    page = output.read_text()
    data = injected(page, "spend-data")
    metadata = injected(page, "build-data")
    assert get_account(data, "Codex")["all"]["usd"] == 4000.0
    assert "Codex" not in data["daily"]["series"]
    assert metadata["codex_history_note"] == "60-day history unknown"
    assert build.codex_history_outside_percent(build.safe_spend(payload)) is None

    complete = build.safe_spend(spend_payload())
    assert build.codex_history_outside_percent(complete) == 99
    assert build.codex_history_note(complete) == "99% outside the dated 60-day series"


def test_curve_metadata_includes_an_account_with_only_fable_activity(tmp_path):
    payload = spend_payload()
    payload["accounts"].append({"name": "Fable Only", "provider": "Claude", "all": bucket(5.0)})
    payload["daily"]["series"]["Fable Only:fable"] = [0.0] * 59 + [5.0]
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(payload))
    db = make_db(tmp_path / "samples.db")
    output = tmp_path / "spend.html"

    build.build(source, db, output, TEMPLATE, NOW)

    page = output.read_text()
    data = injected(page, "spend-data")
    metadata = injected(page, "build-data")
    assert "Fable Only:fable" in data["daily"]["series"]
    assert "Fable Only" in metadata["curve_accounts"]


def get_account(data: dict, name: str) -> dict:
    return next(account for account in data["accounts"] if account["name"] == name)


def test_read_meters_marks_passed_resets_presumed_and_limits_here_to_claude(tmp_path):
    payload = build.safe_spend(spend_payload())
    db = make_db(tmp_path / "samples.db")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-19T17:48:11Z", "Claude", "work", "Alpha", "session",
         47.0, None, 300, "2026-09-19T21:50:00Z", "2026-09-19T17:48:11Z", 1),
    )
    con.commit()
    con.close()

    meters, skipped = build.read_meters(db, payload, NOW)
    assert skipped == []
    session = meters["Alpha"]["meters"]["session"]
    assert session["presumed"] is True and session["pct"] == 0.0 and session["was"] == 47.0
    assert build.parse_iso(session["resets"]) > NOW
    assert meters["Codex"]["here"] is False


def test_sampled_provider_meter_ids_survive_the_public_spend_projection(tmp_path):
    payload = spend_payload()
    payload["meters"]["Codex/codex_bengalfox"] = {
        "usd": 8.0, "start": "2026-09-24T11:00:00Z", "end": "2026-09-24T16:00:00Z",
    }
    assert "Codex/codex_bengalfox" not in build.safe_spend(payload)["meters"]
    source = tmp_path / "spend.json"
    source.write_text(json.dumps(payload))
    db = make_db(tmp_path / "samples.db")
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-24T11:59:00Z", "Codex", "codex", "Codex", "codex_bengalfox",
             24.0, None, 300, "2026-09-24T16:00:00Z", "2026-09-24T11:59:00Z", 1),
        )
    output = tmp_path / "spend.html"

    build.build(source, db, output, TEMPLATE, NOW)

    data = injected(output.read_text(), "spend-data")
    meters = injected(output.read_text(), "meter-data")
    assert data["meters"]["Codex/codex_bengalfox"]["usd"] == 8.0
    assert meters["Codex"]["meters"]["codex_bengalfox"]["minutes"] == 300


def test_read_meters_resolves_current_names_from_alias_and_skips_mislabeled_rows(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({
        "work": {"email": "work@example.test"},
        "personal": {"email": "personal@example.test"},
    }))
    monkeypatch.setattr(build.glideslope, "CLAUDE_ROSTER", roster)
    monkeypatch.setattr(build.glideslope, "CLAUDE_CALL_SIGNS", {
        "work": "Renamed One", "personal": "Renamed Two",
    })
    db = tmp_path / "samples.db"
    con = sqlite3.connect(db)
    con.execute(SAMPLE_SCHEMA)
    con.executemany(
        "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-09-24T11:50:00Z", "Claude", "work", "Old Work", "weekly_all",
             25.0, None, 10080, "2026-09-27T03:00:00Z", "2026-09-24T11:50:00Z", 1),
            ("2026-09-24T11:55:00Z", "Claude", "personal", "Old Personal", "weekly_all",
             35.0, None, 10080, "2026-09-27T03:00:00Z", "2026-09-24T11:55:00Z", 0),
            ("2026-09-24T11:59:00Z", "Claude", "personal", "Renamed One", "weekly_all",
             77.0, None, 10080, "2026-09-27T03:00:00Z", "2026-09-24T11:59:00Z", 0),
        ],
    )
    con.commit()
    con.close()
    accounts = {"accounts": [
        {"name": "Renamed One", "provider": "Claude"},
        {"name": "Renamed Two", "provider": "Claude"},
    ]}

    meters, skipped = build.read_meters(db, accounts, NOW)

    assert meters["Renamed One"]["meters"]["weekly_all"]["pct"] == 25.0
    assert meters["Renamed Two"]["meters"]["weekly_all"]["pct"] == 35.0
    assert skipped == ["Renamed One/weekly_all: row carries Renamed Two's store alias"]
    assert "work" not in json.dumps(meters) and "personal" not in json.dumps(meters)
    assert "Old Work" not in json.dumps(meters) and "Old Personal" not in json.dumps(meters)
