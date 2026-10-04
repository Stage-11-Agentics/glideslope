"""Hermetic tests for the public spend producer and its sampler integration."""

import datetime as dt
import importlib.util
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import glideslope
import pricing
import sampler
import spend


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "views" / "deck-src" / "build.py"
SPEC = importlib.util.spec_from_file_location("glideslope_spend_test_deck_builder", BUILDER_PATH)
assert SPEC and SPEC.loader
deck_builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deck_builder)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(tool, model, when, *, key, inp=0, out=0, speed=None, prompt=None):
    return {
        "tool": tool,
        "model": model,
        "ts": int(when.timestamp() * 1000),
        "in": inp,
        "out": out,
        "cr": 0,
        "cw5": 0,
        "cw1": 0,
        "think": 0,
        "speed": speed,
        "speed_src": "api" if speed else None,
        "tier": None,
        "prompt": inp if prompt is None else prompt,
        "key": key,
        "machine": "laptop",
    }


def _sample_db(path: Path, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    old = now - dt.timedelta(days=10)
    middle = now - dt.timedelta(days=1)
    recent = now - dt.timedelta(minutes=30)
    future_reset = _iso(now + dt.timedelta(days=6))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE samples ("
            "provider TEXT, account TEXT, meter TEXT, used_percent REAL, window_minutes INTEGER,"
            "resets_at TEXT, observed_at TEXT, active INTEGER, plan TEXT,"
            "UNIQUE(provider, account, meter, observed_at))"
        )
        rows = [
            ("Claude", "work", "weekly_all", 20.0, 10080, future_reset, old, 1, "Max 20x"),
            ("Claude", "personal", "weekly_all", 35.0, 10080, future_reset, middle, 1, "Max 20x"),
            ("Claude", "work", "weekly_all", 25.0, 10080, future_reset, recent, 1, "Max 20x"),
            ("Claude", "work", "weekly_fable", 45.0, 10080, future_reset, recent, 1, "Max 20x"),
            ("Codex", "codex", "weekly_all", 50.0, 10080, future_reset, recent, 0, "20x"),
            ("Grok", "grok", "weekly_all", 40.0, 10080, future_reset, recent, 0, "SuperGrok"),
        ]
        con.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)", [
            (provider, account, meter, pct, minutes, reset, _iso(observed), active, plan)
            for provider, account, meter, pct, minutes, reset, observed, active, plan in rows
        ])
    return old, middle, recent


def _records(old: dt.datetime, middle: dt.datetime, recent: dt.datetime) -> dict:
    requests = [
        _request("claude", "claude-opus-5-5", old - dt.timedelta(hours=1),
                 key="before-first", inp=100, out=100, speed="standard"),
        _request("claude", "claude-sonnet-4-5", old + dt.timedelta(hours=1),
                 key="alpha-old", inp=1000, out=500, speed="standard"),
        _request("claude", "claude-opus-5-5", middle + dt.timedelta(hours=1),
                 key="bravo-fast", inp=1_000_000, out=100_000, speed="fast"),
        _request("claude", "claude-fable-5-1", recent + dt.timedelta(minutes=1),
                 key="alpha-fable", inp=1_000_000, out=100_000, speed="standard"),
        _request("codex", "gpt-6.1-sol", recent, key="codex-one", inp=1000, out=1000,
                 speed="standard"),
        _request("grok", "grok-4.2-fast", recent, key="grok-one", inp=500, out=300),
    ]
    return {
        "records": requests,
        "grok_cost_ticks": {"grok-one": 1_000_000_000_000},
        "grok_model_calls": {"grok-one": 7},
        "collected_at": {"laptop": _iso(recent)},
    }


def _bucket_checks(data):
    bucket_dicts = list(data["totals"].values())
    for account in data["accounts"]:
        bucket_dicts.extend(account[horizon] for horizon in spend.HORIZONS)
        bucket_dicts.extend(account["models"].values())
        if account.get("window"):
            bucket_dicts.append(account["window"])
    for group in data["providers"].values():
        bucket_dicts.extend(group.values())
    for machine in data["machines"].values():
        bucket_dicts.extend(machine.values())
    bucket_dicts.extend(data["meters"].values())
    for bucket in bucket_dicts:
        assert "speed" in bucket
        assert "premium_usd" in bucket


def test_build_attribution_buckets_pricing_and_grok_model_calls(tmp_path, monkeypatch):
    now = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
    db = tmp_path / "samples.db"
    old, middle, recent = _sample_db(db, now)
    collection = _records(old, middle, recent)
    lite = {
        "gpt-6.1-sol": {
            "input_cost_per_token": 0.00001,
            "output_cost_per_token": 0.00002,
            "cache_read_input_token_cost": 0.000001,
            "cache_creation_input_token_cost": 0.00001,
        }
    }
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: 1.5)

    data = spend.build(collection, now=now, db=db, pricer=pricing.Pricer(lite=lite))
    accounts = {account["name"]: account for account in data["accounts"]}

    assert set(data) == {
        "generated_at", "collected_at", "first_request_at", "attribution", "totals",
        "machines", "accounts", "leverage", "providers", "meters", "daily",
        "model_tokens", "pricing",
    }
    assert accounts["unattributed"]["all"]["requests"] == 1
    assert accounts["Alpha"]["d30"]["requests"] == 2
    assert accounts["Bravo"]["d7"]["speed"]["fast"]["premium_usd"] == 6.0
    assert accounts["Alpha"]["window"]["fable_usd"] == 15.0
    assert data["meters"]["Alpha/weekly_fable"]["fable_tokens"] == 1_100_000
    assert data["providers"]["Grok"]["all"]["usd"] == 100.0
    assert data["providers"]["Grok"]["all"]["requests"] == 7
    grok_model = next(row for row in data["model_tokens"] if row["provider"] == "Grok")
    assert grok_model["all"]["requests"] == 7
    assert data["pricing"]["litellm_age_h"] == 1.5
    assert data["collected_at"] == collection["collected_at"]
    _bucket_checks(data)


def test_pre_sample_claude_request_stays_unattributed(tmp_path, monkeypatch):
    now = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
    db = tmp_path / "samples.db"
    old, _middle, recent = _sample_db(db, now)
    request = _request("claude", "claude-sonnet-4-5", old - dt.timedelta(hours=1),
                       key="pre-sample", inp=1000, out=100)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)

    data = spend.build({"records": [request]}, now=now, db=db, pricer=pricing.Pricer(lite={}))
    accounts = {account["name"]: account for account in data["accounts"]}
    assert accounts["unattributed"]["all"]["requests"] == 1
    assert accounts["Alpha"]["all"]["requests"] == 0
    assert spend._iso_ms(data["attribution"]["laptop"]) == int(old.timestamp() * 1000)
    assert recent < now


def test_produced_spend_file_flows_through_the_real_deck_builder(tmp_path, monkeypatch):
    now = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
    old, middle, recent = now - dt.timedelta(days=10), now - dt.timedelta(days=2), now - dt.timedelta(minutes=30)
    data = spend.build(
        _records(old, middle, recent), now=now, db=tmp_path / "missing.db",
        pricer=pricing.Pricer(lite={"gpt-6.1-sol": {"input_cost_per_token": 1e-5,
                                                      "output_cost_per_token": 2e-5}}),
    )
    spend_file = tmp_path / "store" / "spend.json"
    spend._atomic_json(data, spend_file)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)
    original_spend_view = deck_builder.spend_view
    monkeypatch.setattr(deck_builder, "spend_view", lambda path=None: original_spend_view(spend_file))
    out = tmp_path / "deck.html"
    popup = tmp_path / "popup.html"
    with redirect_stdout(StringIO()):
        snapshot = deck_builder.write_deck(
            {"accounts": [], "warnings": [], "remote_seats": {}}, out, popup
        )
    assert snapshot["spend"]["totals"]["all"]["usd"] == data["totals"]["all"]["usd"]
    assert out.is_file() and popup.is_file()
    assert "API-equivalent spend" in out.read_text(encoding="utf-8")


def test_spend_prices_command_routes_to_the_existing_pricing_cli(monkeypatch):
    with patch.object(pricing, "prices_cli", return_value=0) as prices_cli:
        assert spend.main(["prices", "--refresh"]) == 0
    prices_cli.assert_called_once_with(force=True)


def test_no_collect_prices_the_saved_minimal_collection_without_reading_transcripts(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setattr(glideslope, "STORE_DIR", store)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)
    saved = {
        "records": [],
        "grok_cost_ticks": {},
        "grok_model_calls": {},
        "collected_at": {"laptop": "2026-10-04T12:00:00Z"},
    }
    spend._atomic_json(saved, store / "spend" / "last-collection.json")
    with patch.object(spend.spend_collect, "collect", side_effect=AssertionError("collected")) as collect, \
            patch.object(pricing, "refresh_prices", return_value="fresh"):
        assert spend._produce(no_collect=True) == 0
    collect.assert_not_called()
    assert json.loads((store / "spend.json").read_text(encoding="utf-8"))["totals"]["all"]["requests"] == 0


def test_sampler_spend_cadence_is_fifteen_minutes_and_nonblocking(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(sampler, "SPEND_LAST_ATTEMPT", tmp_path / "store" / "last")
    monkeypatch.setattr(sampler, "SPEND_CADENCE_LOCK", tmp_path / "store" / "lock")
    monkeypatch.setattr(sampler, "SPEND_LOG", tmp_path / "store" / "spend.log")
    monkeypatch.setattr(sampler, "SPEND_PRODUCER", tmp_path / "spend.py")
    with patch.object(sampler.subprocess, "Popen") as popen:
        assert sampler.run_spend_if_due(now_seconds=1_800_000_000)
        assert not sampler.run_spend_if_due(now_seconds=1_800_000_000 + 14 * 60)
        assert sampler.run_spend_if_due(now_seconds=1_800_000_000 + 15 * 60)
    assert popen.call_count == 2
    assert popen.call_args.kwargs["start_new_session"] is True


def test_sample_commits_when_spend_launch_fails(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setattr(sampler, "STORE_DIR", store)
    monkeypatch.setattr(sampler, "DB_PATH", store / "samples.db")
    monkeypatch.setattr(sampler, "SPEND_LAST_ATTEMPT", store / "last")
    monkeypatch.setattr(sampler, "SPEND_CADENCE_LOCK", store / "lock")
    monkeypatch.setattr(sampler, "SPEND_LOG", store / "spend.log")
    position_time = "2026-10-04T12:00:00Z"
    monkeypatch.setattr(sampler, "collect", lambda: {
        "generated_at": position_time,
        "accounts": [{
            "provider": "Claude", "account": "work", "display": "Alpha", "active": True,
            "observed_at": position_time,
            "limits": [{"meter_id": "weekly_all", "used_percent": 12.0,
                        "window_minutes": 10080, "resets_at": "2026-10-11T12:00:00Z"}],
        }],
    })
    monkeypatch.setattr(sampler, "rebuild_deck", lambda _position: "deck rebuilt")
    monkeypatch.setattr(sampler, "rebuild_history", lambda: "history rebuilt")
    monkeypatch.setattr(sampler, "fire_alerts", lambda _position: [])
    stderr = StringIO()
    with patch.object(sampler.subprocess, "Popen", side_effect=OSError("forced")), redirect_stderr(stderr):
        assert sampler.main() == 0

    with sqlite3.connect(store / "samples.db") as con:
        assert con.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    assert "spend producer could not be started" in stderr.getvalue()
