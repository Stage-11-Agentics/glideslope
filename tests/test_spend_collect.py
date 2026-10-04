"""Hermetic tests for the request-only spend collector."""

import ast
import hashlib
import json
import shutil
import sqlite3
import tomllib
from pathlib import Path

import pytest

import glideslope
import spend_collect as collector
from pricing import CODEX_SPEED, SpeedLedger


FIXTURES = Path(__file__).parent / "fixtures" / "spend"


def _copy(source: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(FIXTURES / source, target))


def _create_codex_logs(codex_home: Path) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    database = codex_home / "logs_fixture.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript((FIXTURES / "codex-logs.sql").read_text())
    return database


def _request(key: str, timestamp: int, tier=None) -> dict:
    request = {field: None for field in collector.RECORD_FIELDS}
    request.update({
        "tool": "codex", "model": "gpt-6.1-sol", "ts": timestamp, "in": 1, "out": 1,
        "cr": 0, "cw5": 0, "cw1": 0, "think": 0, "speed": CODEX_SPEED.get(tier) if tier else None,
        "speed_src": "rollout" if tier else None, "tier": tier, "prompt": 1,
        "key": key, "machine": "test-machine",
    })
    return request


def test_claude_stream_and_resume_dedup_and_profile_roots(tmp_path):
    home = tmp_path / "home"
    default_projects = home / ".claude" / "projects"
    profile_projects = home / ".claude-profiles" / "profile-one" / "projects"
    _copy("claude-stream-a.jsonl", default_projects / "session-a.jsonl")
    _copy("claude-resumed.jsonl", default_projects / "session-resumed.jsonl")
    _copy("claude-subagent.jsonl", profile_projects / "subagents" / "agent-one.jsonl")
    linked_projects = home / ".claude-profiles" / "profile-linked" / "projects"
    linked_projects.parent.mkdir(parents=True)
    linked_projects.symlink_to(default_projects, target_is_directory=True)

    assert collector.claude_project_roots(home) == [default_projects, profile_projects]
    result = collector.collect(
        home=home,
        machine="test-machine",
        speed_ledger=SpeedLedger(tmp_path / "speed-ledger.json"),
    )

    assert len(result.records) == 2
    main = next(request for request in result.records if request["key"] == "message-one|request-one")
    subagent = next(request for request in result.records if request["key"] == "message-subagent|request-subagent")
    assert main["out"] == 12
    assert main["ts"] == collector._timestamp_ms("2026-10-01T00:00:01Z")
    assert main["think"] == 4
    assert main["prompt"] == 95
    assert main["speed"] == "fast"
    assert subagent["speed"] is None
    assert subagent["speed_src"] is None
    assert all(set(request) == set(collector.RECORD_FIELDS) for request in result.records)
    assert result.grok_cost_ticks == {}


def test_per_file_cache_skips_unchanged_reparses_changes_and_drops_deleted_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    transcript = _copy("claude-stream-a.jsonl", home / ".claude" / "projects" / "one.jsonl")
    cache_dir = tmp_path / "store" / "spend-cache"
    parsed = []
    original = collector._claude_file

    def count_parse(path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(collector, "_claude_file", count_parse)
    ledger = SpeedLedger(tmp_path / "speed-ledger.json")

    first = collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    assert len(first.records) == 1
    assert len(parsed) == 1
    entries = list(cache_dir.glob("*.json"))
    assert len(entries) == 1
    cached_text = entries[0].read_text(encoding="utf-8")
    assert str(home) not in cached_text
    assert "prompt text" not in cached_text

    warm = collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    assert warm.records == first.records
    assert len(parsed) == 1

    transcript.write_text(transcript.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    assert changed.records == first.records
    assert len(parsed) == 2

    transcript.unlink()
    deleted = collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    assert deleted.records == []
    assert list(cache_dir.glob("*.json")) == []


def test_cache_version_tracks_collector_source():
    assert collector.FILE_CACHE_VERSION == hashlib.sha256(Path(collector.__file__).read_bytes()).hexdigest()


def test_per_file_cache_reparses_a_stale_version(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _copy("claude-stream-a.jsonl", home / ".claude" / "projects" / "one.jsonl")
    cache_dir = tmp_path / "store" / "spend-cache"
    parsed = []
    original = collector._claude_file

    def count_parse(path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(collector, "_claude_file", count_parse)
    ledger = SpeedLedger(tmp_path / "speed-ledger.json")
    collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    cache_path = next(cache_dir.glob("*.json"))
    entry = json.loads(cache_path.read_text(encoding="utf-8"))
    entry["version"] = "stale-parser-version"
    cache_path.write_text(json.dumps(entry), encoding="utf-8")

    collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)

    assert len(parsed) == 2


def test_per_file_cache_reparses_a_wrong_kind_even_with_valid_other_kind_shape(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _copy("claude-stream-a.jsonl", home / ".claude" / "projects" / "one.jsonl")
    cache_dir = tmp_path / "store" / "spend-cache"
    parsed = []
    original = collector._claude_file

    def count_parse(path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(collector, "_claude_file", count_parse)
    ledger = SpeedLedger(tmp_path / "speed-ledger.json")
    collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)
    cache_path = next(cache_dir.glob("*.json"))
    entry = json.loads(cache_path.read_text(encoding="utf-8"))
    entry["kind"] = "grok"
    entry["payload"].update(grok_cost_ticks={}, grok_model_calls={})
    cache_path.write_text(json.dumps(entry), encoding="utf-8")

    collector.collect(home=home, machine="cache-test", speed_ledger=ledger, cache_dir=cache_dir)

    assert len(parsed) == 2


def test_spend_cache_lives_under_the_configured_store(tmp_path, monkeypatch):
    store = tmp_path / "configured-store"
    monkeypatch.setattr(glideslope, "STORE_DIR", store)

    assert collector._spend_cache_dir() == store / "spend-cache"


def test_saved_collection_rehydrates_from_per_file_cache_without_transcripts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _copy("claude-stream-a.jsonl", home / ".claude" / "projects" / "one.jsonl")
    cache_dir = tmp_path / "store" / "spend-cache"
    expected = collector.collect(
        home=home, machine="cache-test", speed_ledger=SpeedLedger(tmp_path / "speed-ledger.json"),
        cache_dir=cache_dir,
    )
    monkeypatch.setattr(collector, "_claude_file", lambda _path: pytest.fail("read transcript"))
    monkeypatch.setattr(collector, "codex_tier_log", lambda _path: pytest.fail("read Codex log"))

    cached = collector.load_cached_collection(
        home=home, machine="cache-test", cache_dir=cache_dir,
        speed_ledger=SpeedLedger(tmp_path / "cached-speed-ledger.json"),
    )

    assert cached is not None
    assert cached.records == expected.records


def test_codex_live_archive_selection_tiers_and_read_only_log_gap(tmp_path):
    home = tmp_path / "home"
    _copy("codex-rollout.jsonl", home / ".codex" / "sessions" / "date" / "thread-main.jsonl")
    _copy("codex-archived.jsonl", home / ".codex" / "archived_sessions" / "thread-main.jsonl")
    database = _create_codex_logs(home / ".codex")
    before = database.stat().st_mtime_ns

    rollout_id, rollout_requests = collector._codex_file(FIXTURES / "codex-rollout.jsonl")
    assert rollout_id == "thread-main"
    assert [request["tier"] for request in rollout_requests] == ["default", "priority", "default"]
    assert [request["prompt"] for request in rollout_requests] == [1000, 1200, 1400]

    observations = collector.codex_tier_log(home / ".codex")
    assert observations["thread-main"] == [(800, "default"), (1500, "priority"), (3500, "default")]

    ledger_path = tmp_path / "speed-ledger.json"
    result = collector.collect(
        home=home,
        machine="test-machine",
        speed_ledger=SpeedLedger(ledger_path),
    )
    codex = [request for request in result.records if request["tool"] == "codex"]
    assert len(codex) == 4
    assert [request["key"] for request in codex] == ["thread-main|0", "thread-main|1", "thread-main|2", "thread-main|3"]
    expected_tiers = [
        ("default", "standard", "log"),
        ("priority", "fast", "log"),
        ("priority", "fast", "log"),
        ("default", "standard", "log"),
    ]
    assert [(request["tier"], request["speed"], request["speed_src"]) for request in codex] == expected_tiers
    assert ledger_path.is_file()
    assert set(json.loads(ledger_path.read_text(encoding="utf-8"))) == {"test-machine|thread-main"}
    assert database.stat().st_mtime_ns == before

    database.unlink()
    restored = collector.collect(
        home=home,
        machine="test-machine",
        speed_ledger=SpeedLedger(ledger_path),
    )
    restored_codex = [request for request in restored.records if request["tool"] == "codex"]
    assert [(request["tier"], request["speed"], request["speed_src"]) for request in restored_codex] == expected_tiers


def test_codex_log_guard_log_next_refusal_and_rollout_fallback():
    log_requests = [_request("thread|0", 1000), _request("thread|1", 2000)]
    assert collector.stamp_codex_speed(log_requests, {"thread": [(1500, "priority")]}) == 2
    assert (log_requests[0]["tier"], log_requests[0]["speed"], log_requests[0]["speed_src"]) == (
        "priority", "fast", "log-next",
    )
    assert log_requests[1]["speed_src"] == "log"

    guarded = [_request("thread|0", 1000), _request("thread|1", 2000, "priority")]
    collector.stamp_codex_speed(guarded, {"thread": [(1500, "priority")]})
    assert guarded[0]["tier"] is None
    assert guarded[0]["speed_src"] is None
    assert guarded[1]["speed_src"] == "log"

    fallback = _request("other-thread|0", 3000, "ultrafast")
    collector.stamp_codex_speed([fallback], {})
    assert fallback["tier"] == "ultrafast"
    assert fallback["speed"] == "ultrafast"
    assert fallback["speed_src"] == "rollout"


def test_speed_ledger_restores_tier_after_log_expiry(tmp_path):
    path = tmp_path / "speed-ledger.json"
    first = SpeedLedger(path)
    logged = _request("thread|3", 1000, "priority")
    logged.update(speed=CODEX_SPEED["priority"], speed_src="log", machine="test-machine")
    first.apply(logged)
    first.save()

    restored = _request("thread|3", 1000, "default")
    restored.update(speed="standard", speed_src="rollout", machine="test-machine")
    SpeedLedger(path).apply(restored)
    assert (restored["tier"], restored["speed"], restored["speed_src"]) == ("priority", "fast", "log")


def test_codex_merged_delta_uses_last_request_prompt_size():
    session_id, requests = collector._codex_file(FIXTURES / "codex-deltas.jsonl")
    assert session_id == "thread-deltas"
    assert len(requests) == 2
    assert requests[1]["in"] == 400
    assert requests[1]["cr"] == 200
    assert requests[1]["out"] == 100
    assert requests[1]["prompt"] == 450


def test_grok_two_model_costs_stay_outside_the_request_shape(tmp_path):
    home = tmp_path / "home"
    _copy("grok-updates.jsonl", home / ".grok" / "sessions" / "worker-one" / "session-one" / "updates.jsonl")
    _copy("grok-updates.jsonl", home / ".grok" / "sessions" / "worker-two" / "session-one" / "updates.jsonl")
    result = collector.collect(
        home=home,
        machine="test-machine",
        speed_ledger=SpeedLedger(tmp_path / "speed-ledger.json"),
    )
    grok = [request for request in result.records if request["tool"] == "grok"]

    assert [request["model"] for request in grok] == ["grok-4.2", "grok-4.2-fast"]
    assert grok[0]["prompt"] == 100
    assert (grok[0]["in"], grok[0]["cr"], grok[0]["cw5"], grok[0]["out"], grok[0]["think"]) == (85, 10, 5, 20, 3)
    assert result.grok_cost_ticks == {
        "session-one|prompt-one|grok-4.2": 123456789,
        "session-one|prompt-one|grok-4.2-fast": 987654321,
    }
    assert result.grok_model_calls == {
        "session-one|prompt-one|grok-4.2": 7,
        "session-one|prompt-one|grok-4.2-fast": 1,
    }
    assert all(set(request) == set(collector.RECORD_FIELDS) for request in grok)
    assert set(result.payload()) == {"records", "grok_cost_ticks", "grok_model_calls"}


def test_unreadable_codex_tier_log_emits_one_path_free_line(tmp_path, capsys):
    database = tmp_path / "logs_broken.sqlite"
    database.write_text("not a sqlite database", encoding="utf-8")

    assert collector.codex_tier_log(tmp_path) == {}
    captured = capsys.readouterr()
    assert captured.err == "[glideslope spend] unreadable Codex tier log; skipping database\n"
    assert str(database) not in captured.err


def test_py_modules_include_local_import_dependencies():
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    modules = set(config["tool"]["setuptools"]["py-modules"])
    local_modules = {path.stem for path in root.glob("*.py")}

    missing = set()
    for module in modules:
        source = root / (module + ".py")
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported = {node.module.split(".", 1)[0]}
            else:
                continue
            missing.update((module, dependency) for dependency in imported & local_modules if dependency not in modules)

    assert not missing, "listed modules import local modules absent from py-modules: %s" % sorted(missing)


def test_worker_cap_requires_a_positive_integer(monkeypatch, tmp_path):
    monkeypatch.setenv(collector.WORKERS_ENV, "0")
    with pytest.raises(ValueError, match=collector.WORKERS_ENV):
        collector.collect(home=tmp_path / "missing", speed_ledger=SpeedLedger(tmp_path / "speed-ledger.json"))
