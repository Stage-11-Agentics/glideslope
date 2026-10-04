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

import claude_account
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
    assert accounts["Bravo"]["d7"]["premium_usd"] == 6.0
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


def test_steady_active_samples_keep_claude_attribution_fresh(tmp_path, monkeypatch):
    start = dt.datetime(2026, 10, 1, 0, tzinfo=dt.timezone.utc)
    now = start + dt.timedelta(hours=49)
    db = tmp_path / "samples.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE samples ("
            "provider TEXT, account TEXT, meter TEXT, used_percent REAL, window_minutes INTEGER,"
            "resets_at TEXT, observed_at TEXT, active INTEGER, plan TEXT,"
            "UNIQUE(provider, account, meter, observed_at))"
        )
        con.executemany(
            "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("Claude", "test-alias", "weekly_all", 20.0, 10080,
                 _iso(start + dt.timedelta(days=7)), _iso(start + dt.timedelta(hours=hour)), 1, "Max")
                for hour in (0, 12, 24, 36, 48)
            ],
        )
    monkeypatch.setattr(glideslope, "CLAUDE_CALL_SIGNS", {"test-alias": "Test account"})

    timelines, *_ = spend._read_sample_state(db, int(now.timestamp() * 1000))
    request_at = int((start + dt.timedelta(hours=36)).timestamp() * 1000)

    assert spend._account_at(timelines["local"], request_at) == "Test account"


def _sampled_login_db(tmp_path, monkeypatch, samples, call_signs, switches=()):
    start = dt.datetime(2026, 10, 1, 0, tzinfo=dt.timezone.utc)
    roster = {
        "work": {"email": "person@example.test", "tier": "default_claude_max_20x"},
        "personal": {"email": "other@example.test", "tier": "default_claude_max_20x"},
    }
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    monkeypatch.setattr(glideslope, "CLAUDE_ROSTER", roster_path)
    monkeypatch.setattr(glideslope, "CLAUDE_CALL_SIGNS", dict(call_signs))
    db = tmp_path / "samples.db"
    with sqlite3.connect(db) as con:
        con.executescript(sampler.SCHEMA)
        for hour, alias in samples:
            observed = start + dt.timedelta(hours=hour)
            display = glideslope.call_sign(alias, roster[alias]["email"])
            position = {"accounts": [{
                "provider": "Claude", "account": alias, "display": display, "active": True,
                "observed_at": _iso(observed), "plan": "Max 20x",
                "limits": [{"meter_id": "weekly_all", "used_percent": 20.0,
                            "window_minutes": 10080,
                            "resets_at": _iso(start + dt.timedelta(days=7))}],
            }]}
            con.executemany(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                sampler.rows_from(position, _iso(observed)),
            )
    # A detached producer starts with the original config, not the position
    # process's runtime alias additions.
    monkeypatch.setattr(glideslope, "CLAUDE_CALL_SIGNS", dict(call_signs))
    switch_log = tmp_path / "switch-log.jsonl"
    switch_log.write_text("".join(json.dumps({
        "at": _iso(start + dt.timedelta(hours=hour)), "to": alias,
    }) + "\n" for hour, alias in switches), encoding="utf-8")
    return start, db, switch_log


@pytest.mark.parametrize(
    ("call_signs", "expected"),
    [({}, "Alpha"), ({"person@example.test": "Work account"}, "Work account")],
)
def test_sampled_account_resolves_default_and_email_call_signs(
    tmp_path, monkeypatch, call_signs, expected
):
    start, db, _switch_log = _sampled_login_db(
        tmp_path, monkeypatch, [(0, "work")], call_signs
    )

    timelines, *_ = spend._read_sample_state(db, int((start + dt.timedelta(hours=1)).timestamp() * 1000))

    assert spend._account_at(timelines["local"], int((start + dt.timedelta(minutes=10)).timestamp() * 1000)) == expected
    assert expected in spend._account_names()


def test_switch_log_moves_attribution_between_samples(tmp_path, monkeypatch):
    start, db, switch_log = _sampled_login_db(
        tmp_path, monkeypatch, [(0, "work"), (24, "personal")], {},
        switches=[(12, "personal")],
    )

    timelines, *_ = spend._read_sample_state(
        db, int((start + dt.timedelta(hours=25)).timestamp() * 1000), switch_log=switch_log
    )

    assert spend._account_at(timelines["local"], int((start + dt.timedelta(hours=18)).timestamp() * 1000)) == "Bravo"


@pytest.mark.parametrize(
    "samples_db_state", ["absent", "no_samples_table", "sqlite_error"]
)
def test_switch_log_attributes_without_usable_samples_db(
    tmp_path, monkeypatch, samples_db_state
):
    start, db, switch_log = _sampled_login_db(
        tmp_path, monkeypatch, [], {}, switches=[(0, "work")]
    )
    if samples_db_state == "absent":
        db.unlink()
        assert not db.exists()
    elif samples_db_state == "no_samples_table":
        with sqlite3.connect(db) as con:
            con.execute("DROP TABLE samples")
    else:
        db.write_text("not a sqlite database", encoding="utf-8")
    monkeypatch.setattr(claude_account, "SWITCH_LOG", switch_log)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)

    request_at = start + dt.timedelta(hours=1)
    data = spend.build(
        {"records": [_request(
            "claude", "claude-sonnet-4-5", request_at, key="switch-only", inp=100
        )]},
        now=request_at,
        db=db,
        pricer=pricing.Pricer(lite={}),
    )
    accounts = {account["name"]: account for account in data["accounts"]}
    expected = spend._alias_names()["work"]

    assert accounts[expected]["all"]["requests"] == 1
    assert accounts["unattributed"]["all"]["requests"] == 0
    assert spend._iso_ms(data["attribution"][glideslope.LOCAL_SATELLITE]) == int(
        start.timestamp() * 1000
    )


def test_pre_sample_switch_is_earliest_attribution_evidence(tmp_path, monkeypatch):
    start, db, switch_log = _sampled_login_db(
        tmp_path, monkeypatch, [(24, "work")], {}, switches=[(12, "work")]
    )
    monkeypatch.setattr(claude_account, "SWITCH_LOG", switch_log)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)
    before_evidence = start + dt.timedelta(hours=11)
    within_switch_age = start + dt.timedelta(hours=18)
    beyond_sample_age = start + dt.timedelta(hours=49)
    now = start + dt.timedelta(hours=50)
    requests = [
        _request("claude", "claude-sonnet-4-5", before_evidence, key="before-switch", inp=100),
        _request("claude", "claude-sonnet-4-5", within_switch_age, key="within-switch-age", inp=100),
        _request("claude", "claude-sonnet-4-5", beyond_sample_age, key="beyond-login-age", inp=100),
    ]

    data = spend.build(
        {"records": requests}, now=now, db=db, pricer=pricing.Pricer(lite={})
    )
    accounts = {account["name"]: account for account in data["accounts"]}

    assert accounts["Alpha"]["all"]["requests"] == 1
    assert accounts["unattributed"]["all"]["requests"] == 2
    assert spend._iso_ms(data["attribution"][glideslope.LOCAL_SATELLITE]) == int(
        (start + dt.timedelta(hours=12)).timestamp() * 1000
    )


@pytest.mark.parametrize(
    ("samples", "now_hour", "request_hour"),
    [([(0, "work"), (12, "work"), (23, "work"), (48, "work")], 49, 40),
     ([(0, "work"), (12, "work"), (23, "work")], 46, 46)],
)
def test_compressed_samples_remain_fresh_from_last_real_observation(
    tmp_path, monkeypatch, samples, now_hour, request_hour
):
    start, db, _switch_log = _sampled_login_db(tmp_path, monkeypatch, samples, {"work": "Work account"})

    timelines, *_ = spend._read_sample_state(db, int((start + dt.timedelta(hours=now_hour)).timestamp() * 1000))

    assert spend._account_at(timelines["local"], int((start + dt.timedelta(hours=request_hour)).timestamp() * 1000)) == "Work account"


def test_first_request_includes_unpriced_activity(monkeypatch):
    now = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
    first = now - dt.timedelta(days=2)
    later = now - dt.timedelta(hours=1)
    collection = {
        "records": [
            _request("grok", "grok-4.2-fast", first, key="unpriced-old", inp=100),
            _request("codex", "gpt-6.1-sol", later, key="priced-new", inp=100),
        ],
        "grok_cost_ticks": {},
        "grok_model_calls": {},
    }
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)

    data = spend.build(
        collection, now=now, db=Path("/missing/samples.db"),
        pricer=pricing.Pricer(lite={"gpt-6.1-sol": {
            "input_cost_per_token": 1e-5, "output_cost_per_token": 2e-5,
        }}),
    )

    assert data["first_request_at"] == spend._iso(int(first.timestamp() * 1000))


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


def _seed_saved_collection(store: Path, request=None):
    cache_dir = store / "spend-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": [request] if request else [],
        "grok_cost_ticks": {},
        "grok_model_calls": {},
    }
    entry = {
        "version": spend.spend_collect.FILE_CACHE_VERSION,
        "kind": "claude",
        "size": 0,
        "mtime_ns": 0,
        "payload": payload,
    }
    (cache_dir / ("a" * 64 + ".json")).write_text(json.dumps(entry), encoding="utf-8")
    collected_at = {glideslope.LOCAL_SATELLITE: "2026-10-04T12:00:00Z"}
    spend._atomic_json({"collected_at": collected_at}, store / "spend" / "collection-state.json")


def test_no_collect_prices_the_saved_minimal_collection_without_reading_transcripts(tmp_path, monkeypatch):
    store = tmp_path / "store"
    home = tmp_path / "home"
    monkeypatch.setattr(glideslope, "STORE_DIR", store)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)
    request = _request("claude", "claude-opus-5-5", dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc),
                       key="saved", inp=100, out=25)
    _seed_saved_collection(store, request)
    with patch.object(spend.spend_collect, "collect", side_effect=AssertionError("collected")) as collect, \
            patch.object(pricing, "refresh_prices", return_value="fresh"):
        assert spend._produce(no_collect=True) == 0
    collect.assert_not_called()
    data = json.loads((store / "spend.json").read_text(encoding="utf-8"))
    assert data["collected_at"] == {glideslope.LOCAL_SATELLITE: "2026-10-04T12:00:00Z"}
    assert data["first_request_at"] == spend._iso(request["ts"])
    assert not (store / "spend" / "last-collection.json").exists()


def test_collection_failure_falls_back_to_the_last_per_file_cache(tmp_path, monkeypatch):
    store = tmp_path / "store"
    home = tmp_path / "home"
    monkeypatch.setattr(glideslope, "STORE_DIR", store)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: None)
    request = _request("claude", "claude-opus-5-5", dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc),
                       key="saved", inp=100, out=25)
    _seed_saved_collection(store, request)

    with patch.object(spend.spend_collect, "collect", side_effect=OSError("forced")), \
            patch.object(pricing, "refresh_prices", return_value="fresh"):
        assert spend._produce() == 0

    data = json.loads((store / "spend.json").read_text(encoding="utf-8"))
    assert data["first_request_at"] == spend._iso(request["ts"])


def test_atomic_json_preserves_old_file_and_cleans_temp_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "spend.json"
    target.write_text('{"old":true}\n', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("forced replace failure")

    monkeypatch.setattr(spend.os, "replace", fail_replace)
    with pytest.raises(OSError, match="forced replace failure"):
        spend._atomic_json({"new": True}, target)

    assert target.read_text(encoding="utf-8") == '{"old":true}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_json_cleans_temp_when_serialization_fails(tmp_path):
    target = tmp_path / "spend.json"

    with pytest.raises(TypeError):
        spend._atomic_json({"not-json": object()}, target)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


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


def test_sampler_spend_producer_can_be_disabled_for_another_writer(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setattr(glideslope, "CONFIG", {"spend": {"producer": False}})
    monkeypatch.setattr(sampler, "STORE_DIR", store)
    monkeypatch.setattr(sampler, "SPEND_LAST_ATTEMPT", store / "last")
    monkeypatch.setattr(sampler, "SPEND_CADENCE_LOCK", store / "lock")
    monkeypatch.setattr(sampler, "SPEND_LOG", store / "spend.log")

    with patch.object(sampler.subprocess, "Popen") as popen:
        assert not sampler.run_spend_if_due(now_seconds=1_800_000_000)

    popen.assert_not_called()
    assert not (store / "last").exists()
    assert not store.exists()


def test_sampler_main_tolerates_scalar_spend_setting_and_rebuilds_views(tmp_path, monkeypatch):
    store = tmp_path / "store"
    config = tmp_path / "config.toml"
    config.write_text("spend = false\n", encoding="utf-8")
    monkeypatch.setattr(glideslope, "CONFIG", glideslope.load_config(config))
    monkeypatch.setattr(sampler, "STORE_DIR", store)
    monkeypatch.setattr(sampler, "DB_PATH", store / "samples.db")
    monkeypatch.setattr(sampler, "SPEND_LAST_ATTEMPT", store / "last")
    monkeypatch.setattr(sampler, "SPEND_CADENCE_LOCK", store / "lock")
    monkeypatch.setattr(sampler, "SPEND_LOG", store / "spend.log")
    monkeypatch.setattr(sampler, "SPEND_PRODUCER", tmp_path / "spend.py")
    monkeypatch.setattr(
        sampler,
        "collect",
        lambda: {"generated_at": "2026-10-04T12:00:00Z", "accounts": []},
    )
    monkeypatch.setattr(sampler, "rebuild_deck", lambda _position: "deck rebuilt")
    monkeypatch.setattr(sampler, "rebuild_history", lambda: "history rebuilt")
    monkeypatch.setattr(sampler, "fire_alerts", lambda _position: [])
    stdout = StringIO()

    with patch.object(sampler.subprocess, "Popen"), redirect_stdout(stdout):
        assert sampler.main() == 0

    assert "deck rebuilt; history rebuilt" in stdout.getvalue()


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
