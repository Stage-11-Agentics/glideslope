#!/usr/bin/env python3
"""Collect minimal, local request records for API-equivalent spend."""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from pricing import CODEX_SPEED, SpeedLedger


RECORD_FIELDS = (
    "tool", "model", "ts", "in", "out", "cr", "cw5", "cw1", "think",
    "speed", "speed_src", "tier", "prompt", "key", "machine",
)
WORKERS_ENV = "GLIDESLOPE_SPEND_WORKERS"
DEFAULT_WORKERS = 2
FILE_CACHE_VERSION = 1
_TIER_RE = re.compile(r'"service_tier"\s*:\s*"([a-z_]+)"')


@dataclass(frozen=True)
class Collection:
    """Request records plus Grok's vendor cost and call count, held outside the record shape."""

    records: list[dict]
    grok_cost_ticks: dict[str, int]
    grok_model_calls: dict[str, int]

    def payload(self) -> dict:
        return {
            "records": self.records,
            "grok_cost_ticks": self.grok_cost_ticks,
            "grok_model_calls": self.grok_model_calls,
        }


def _integer(value, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _timestamp_ms(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _record(values: dict) -> dict:
    if set(values) != set(RECORD_FIELDS):
        raise ValueError("request record fields do not match the public contract")
    return values


def _request_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def claude_project_roots(home: Path | None = None) -> list[Path]:
    """Find the default transcript root and real profile roots without using profile names."""
    home = Path.home() if home is None else Path(home)
    roots = []
    default = home / ".claude" / "projects"
    if default.is_dir():
        roots.append(default)

    profiles = home / ".claude-profiles"
    if profiles.is_dir():
        for projects in sorted(profiles.glob("*/projects")):
            if projects.is_symlink() or not projects.is_dir():
                continue
            roots.append(projects)
    return roots


def _claude_file(path: Path) -> list[dict]:
    requests: dict[str, dict] = {}
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return []
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            message = row.get("message") or {}
            usage = message.get("usage")
            model = message.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue

            message_id = message.get("id") or row.get("uuid")
            request_id = row.get("requestId") or ""
            if not message_id and not request_id:
                continue
            key = "%s|%s" % (message_id or "", request_id)
            cache_creation = usage.get("cache_creation") or {}
            cw_total = _integer(usage.get("cache_creation_input_tokens"))
            cw5 = cache_creation.get("ephemeral_5m_input_tokens")
            cw1 = cache_creation.get("ephemeral_1h_input_tokens")
            if cw5 is None and cw1 is None:
                cw5, cw1 = cw_total, 0
            else:
                cw5, cw1 = _integer(cw5), _integer(cw1)
            uncached = _integer(usage.get("input_tokens"))
            cached = _integer(usage.get("cache_read_input_tokens"))
            prompt = uncached + cached + cw5 + cw1
            details = usage.get("output_tokens_details") or {}
            speed = usage.get("speed")
            timestamp = _timestamp_ms(row.get("timestamp"))
            current = _record({
                "tool": "claude", "model": model, "ts": timestamp,
                "in": uncached, "out": _integer(usage.get("output_tokens")), "cr": cached,
                "cw5": cw5, "cw1": cw1, "think": _integer(details.get("thinking_tokens")),
                "speed": speed, "speed_src": "api" if speed else None, "tier": usage.get("service_tier"),
                "prompt": prompt, "key": key, "machine": "",
            })
            previous = requests.get(key)
            if previous is None:
                requests[key] = current
            else:
                if current["out"] > previous["out"]:
                    previous["out"] = current["out"]
                    previous["think"] = max(previous["think"], current["think"])
                if current["ts"] is not None and (previous["ts"] is None or current["ts"] < previous["ts"]):
                    previous["ts"] = current["ts"]
    return list(requests.values())


def _codex_file(path: Path) -> tuple[str, list[dict]]:
    records: dict[str, dict] = {}
    deltas: list[dict] = []
    session_id = None
    previous_total = None
    model = None
    tier = None
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return path.stem, []
    with handle:
        for line in handle:
            if not any(marker in line[:180] for marker in (
                '"session_meta"', '"turn_context"', '"token_count"',
                '"token_usage_record"', '"thread_settings_applied"',
            )):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            timestamp = _timestamp_ms(row.get("timestamp"))
            kind = row.get("type")
            payload = row.get("payload") or {}
            if kind == "session_meta":
                session_id = payload.get("id") or payload.get("session_id") or session_id
            elif kind == "turn_context":
                model = payload.get("model") or model
            elif kind == "token_usage_record":
                usage = payload.get("usage") or {}
                response_id = payload.get("response_id") or str(timestamp)
                total_input = _integer(usage.get("input_tokens"))
                cached = _integer(usage.get("cached_input_tokens"))
                records[response_id] = {
                    "ts": timestamp,
                    "in": max(total_input - cached, 0),
                    "out": _integer(usage.get("output_tokens")),
                    "cr": cached,
                    "cw5": _integer(usage.get("cache_write_input_tokens")),
                    "cw1": 0,
                    "think": _integer(usage.get("reasoning_output_tokens")),
                    "prompt": total_input,
                    "model": model,
                    "tier": tier,
                }
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage")
                if not isinstance(total, dict) or not total:
                    continue
                total_tokens = _integer(total.get("total_tokens"))
                if previous_total is not None and total_tokens <= _integer(previous_total.get("total_tokens")):
                    continue
                prior = previous_total or {}
                delta = {
                    field: _integer(total.get(field)) - _integer(prior.get(field))
                    for field in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
                }
                previous_total = total
                last = info.get("last_token_usage") or {}
                deltas.append({
                    "ts": timestamp,
                    "prompt": _integer(last.get("input_tokens")) if last.get("input_tokens") is not None else None,
                    "in": max(delta["input_tokens"] - delta["cached_input_tokens"], 0),
                    "out": max(delta["output_tokens"], 0),
                    "cr": max(delta["cached_input_tokens"], 0),
                    "cw5": 0,
                    "cw1": 0,
                    "think": max(delta["reasoning_output_tokens"], 0),
                    "model": model,
                    "tier": tier,
                })
            elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
                tier = (payload.get("thread_settings") or {}).get("service_tier") or tier

    rows = list(records.values()) if records else deltas
    session_id = session_id or path.stem
    requests = []
    for index, row in enumerate(rows):
        raw_tier = row["tier"]
        requests.append(_record({
            "tool": "codex", "model": row["model"] or "codex-unknown", "ts": row["ts"],
            "in": row["in"], "out": row["out"], "cr": row["cr"], "cw5": row["cw5"], "cw1": row["cw1"],
            "think": row["think"], "speed": CODEX_SPEED.get(raw_tier) if raw_tier else None,
            "speed_src": "rollout" if raw_tier else None, "tier": raw_tier,
            "prompt": row["prompt"], "key": "%s|%d" % (session_id, index), "machine": "",
        }))
    return session_id, requests


def codex_tier_log(codex_home: Path) -> dict[str, list[tuple[int, str]]]:
    """Read Codex feedback-tag tiers from matching SQLite logs without opening them for writes."""
    observations: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(codex_home.glob("logs_*.sqlite")):
        uri = "file:%s?mode=ro" % quote(path.as_posix())
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = connection.execute(
                    "SELECT thread_id, ts, ts_nanos, feedback_log_body FROM logs"
                    " WHERE target = 'feedback_tags' AND thread_id IS NOT NULL"
                    " AND feedback_log_body LIKE '%\"service_tier\"%'"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            sys.stderr.write("[glideslope spend] unreadable Codex tier log; skipping database\n")
            continue
        for thread_id, seconds, nanos, body in rows:
            match = _TIER_RE.search(body or "")
            if not match:
                continue
            timestamp = _integer(seconds) * 1000 + _integer(nanos) // 1_000_000
            observations.setdefault(thread_id, []).append((timestamp, match.group(1)))
    for values in observations.values():
        values.sort()
    return observations


def stamp_codex_speed(requests: list[dict], observations: dict[str, list[tuple[int, str]]]) -> int:
    """Stamp log-backed tiers only when rollout state agrees at dispatch and completion."""
    order = sorted(range(len(requests)), key=lambda index: requests[index].get("ts") or 0)
    rollout = [requests[index].get("tier") for index in order]
    timestamps = [requests[index].get("ts") or 0 for index in order]
    stamped = 0

    def same_state(position: int, dispatch_ms: int) -> bool:
        completed = bisect.bisect_left(timestamps, dispatch_ms)
        return completed < len(order) and rollout[completed] == rollout[position]

    for position, index in enumerate(order):
        request = requests[index]
        session_id, separator, _request_index = request["key"].rpartition("|")
        if not separator or not request.get("ts"):
            continue
        sequence = observations.get(session_id)
        if not sequence:
            continue
        before = bisect.bisect_right(sequence, (request["ts"], "\uffff"))
        tier = source = None
        if before and same_state(position, sequence[before - 1][0]):
            tier, source = sequence[before - 1][1], "log"
        elif before < len(sequence) and same_state(position, sequence[before][0]):
            tier, source = sequence[before][1], "log-next"
        if tier is None:
            continue
        request["tier"] = tier
        request["speed"] = CODEX_SPEED.get(tier, tier)
        request["speed_src"] = source
        stamped += 1
    return stamped


def _grok_file(path: Path) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    requests: list[dict] = []
    costs: dict[str, int] = {}
    model_calls: dict[str, int] = {}
    session_id = path.parent.name
    seen_turns = set()
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return requests, costs, model_calls
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            params = row.get("params") or {}
            update = params.get("update") or {}
            usage = update.get("usage")
            prompt_id = update.get("prompt_id")
            timestamp = _integer((params.get("_meta") or {}).get("agentTimestampMs")) or None
            if not isinstance(usage, dict) or not prompt_id or not timestamp:
                continue
            turn_key = (session_id, str(prompt_id))
            if turn_key in seen_turns:
                continue
            seen_turns.add(turn_key)

            per_model = {
                name: value for name, value in (usage.get("modelUsage") or {}).items()
                if isinstance(value, dict)
            }
            if not any(_integer(value.get("totalTokens") or value.get("inputTokens")) for value in per_model.values()):
                per_model = {next(iter(per_model), "grok"): usage}

            for model, value in per_model.items():
                prompt = _integer(value.get("inputTokens"))
                cached_read = _integer(value.get("cachedReadTokens"))
                cache_write = _integer(value.get("cacheCreationTokens"))
                output = _integer(value.get("outputTokens"))
                key = "%s|%s|%s" % (session_id, prompt_id, model)
                requests.append(_record({
                    "tool": "grok", "model": model, "ts": timestamp,
                    "in": max(prompt - cached_read - cache_write, 0), "out": output,
                    "cr": cached_read, "cw5": cache_write, "cw1": 0,
                    "think": _integer(value.get("reasoningTokens")), "speed": None, "speed_src": None,
                    "tier": None, "prompt": prompt, "key": key, "machine": "",
                }))
                raw_cost = value.get("costUsdTicks")
                if raw_cost is not None:
                    costs[key] = _integer(raw_cost)
                model_calls[key] = _integer(value.get("modelCalls")) or 1
    return requests, costs, model_calls


def _worker_count(workers: int | None) -> int:
    raw = os.environ.get(WORKERS_ENV, str(DEFAULT_WORKERS)) if workers is None else workers
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ValueError("%s must be a positive integer" % WORKERS_ENV) from None
    if count < 1:
        raise ValueError("%s must be a positive integer" % WORKERS_ENV)
    return count


def _map_files(function, paths: list[Path], workers: int) -> Iterator:
    """Map with a bounded in-flight window so large collections stay bounded."""
    if not paths:
        return
    limit = min(workers, len(paths))
    with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as pool:
        pending = []
        iterator = iter(paths)
        for _ in range(limit):
            try:
                pending.append(pool.submit(function, next(iterator)))
            except StopIteration:
                break
        while pending:
            future = pending.pop(0)
            yield future.result()
            try:
                pending.append(pool.submit(function, next(iterator)))
            except StopIteration:
                pass


def _cache_identity(path: Path) -> str:
    """Return a stable opaque key; raw transcript paths never enter the cache."""
    try:
        value = str(path.resolve())
    except OSError:
        value = str(path.absolute())
    return hashlib.sha256(os.fsencode(value)).hexdigest()


def _spend_cache_dir() -> Path:
    """Resolve the cache under Glideslope's configured store, never the repository."""
    import glideslope

    return Path(glideslope.STORE_DIR) / "spend-cache"


def _cache_payload_valid(kind: str, payload) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return False
    if any(not isinstance(row, dict) or set(row) != set(RECORD_FIELDS)
           for row in payload["records"]):
        return False
    if kind == "codex":
        return isinstance(payload.get("session_id"), str)
    if kind == "grok":
        return (isinstance(payload.get("grok_cost_ticks"), dict)
                and isinstance(payload.get("grok_model_calls"), dict))
    return kind == "claude"


def _cached_file(path: Path, kind: str, cache_dir: Path, parser):
    """Reuse minimal parsed request records when a source file's size and mtime match."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if not os.access(path, os.R_OK):
        return None
    cache_path = cache_dir / (_cache_identity(path) + ".json")
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (cached.get("version") == FILE_CACHE_VERSION
                and cached.get("kind") == kind
                and cached.get("size") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and _cache_payload_valid(kind, cached.get("payload"))):
            return cached["payload"]
    except (OSError, ValueError, AttributeError):
        pass

    try:
        payload = parser(path)
    except OSError:
        return None
    if not _cache_payload_valid(kind, payload):
        return payload
    entry = {
        "version": FILE_CACHE_VERSION,
        "kind": kind,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "payload": payload,
    }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=cache_dir, prefix=".spend-cache-", delete=False
        ) as handle:
            json.dump(entry, handle, separators=(",", ":"))
            temporary = Path(handle.name)
        os.replace(temporary, cache_path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
    return payload


def _prune_file_cache(cache_dir: Path, active: set[str]) -> None:
    """Drop entries for source files deleted since the prior collection."""
    try:
        for entry in cache_dir.glob("*.json"):
            if entry.stem not in active:
                entry.unlink()
    except OSError:
        # Cache cleanup is an optimization and must not make collection fail.
        pass


def collect(
    home: Path | None = None,
    machine: str | None = None,
    workers: int | None = None,
    speed_ledger: SpeedLedger | None = None,
    cache_dir: Path | None = None,
) -> Collection:
    """Read token usage, reusing unchanged files from a store-local cache."""
    home = Path.home() if home is None else Path(home)
    machine = machine or socket.gethostname().split(".", 1)[0].lower()
    worker_cap = _worker_count(workers)
    cache_dir = Path(cache_dir) if cache_dir is not None else _spend_cache_dir()
    active_cache: set[str] = set()

    claude_requests: dict[str, dict] = {}
    for root in claude_project_roots(home):
        paths = _request_files(root)
        active_cache.update(_cache_identity(path) for path in paths)
        parsed = _map_files(
            lambda path: _cached_file(
                path, "claude", cache_dir,
                lambda item: {"records": _claude_file(item)},
            ),
            paths,
            worker_cap,
        )
        for payload in parsed:
            for request in (payload or {}).get("records", []):
                request["machine"] = machine
                previous = claude_requests.get(request["key"])
                if previous is None:
                    claude_requests[request["key"]] = request
                else:
                    if request["out"] > previous["out"]:
                        previous["out"] = request["out"]
                        previous["think"] = max(previous["think"], request["think"])
                    if request["ts"] is not None and (previous["ts"] is None or request["ts"] < previous["ts"]):
                        previous["ts"] = request["ts"]

    codex_home = home / ".codex"
    codex_files = sorted(
        _request_files(codex_home / "sessions") + _request_files(codex_home / "archived_sessions")
    )
    active_cache.update(_cache_identity(path) for path in codex_files)
    best: dict[str, tuple[int, str, list[dict]]] = {}
    def parse_codex(path: Path) -> dict:
        session_id, records = _codex_file(path)
        return {"session_id": session_id, "records": records}

    parsed_codex = _map_files(
        lambda path: _cached_file(path, "codex", cache_dir, parse_codex), codex_files, worker_cap
    )
    for path, payload in zip(codex_files, parsed_codex):
        if not payload:
            continue
        session_id, requests = payload["session_id"], payload["records"]
        path_key = str(path)
        previous = best.get(session_id)
        if previous is None or len(requests) > previous[0] or (len(requests) == previous[0] and path_key < previous[1]):
            best[session_id] = (len(requests), path_key, requests)

    observations = codex_tier_log(codex_home)
    ledger = speed_ledger if speed_ledger is not None else SpeedLedger()
    codex_requests = []
    for session_id in sorted(best):
        requests = best[session_id][2]
        stamp_codex_speed(requests, observations)
        for request in requests:
            request["machine"] = machine
            ledger.apply(request)
            codex_requests.append(request)
    ledger.save()

    grok_root = home / ".grok" / "sessions"
    grok_files = sorted(grok_root.glob("*/*/updates.jsonl")) if grok_root.is_dir() else []
    active_cache.update(_cache_identity(path) for path in grok_files)
    grok_requests = []
    grok_cost_ticks: dict[str, int] = {}
    grok_model_calls: dict[str, int] = {}
    seen_grok_turns = set()
    def parse_grok(path: Path) -> dict:
        records, costs, model_calls = _grok_file(path)
        return {"records": records, "grok_cost_ticks": costs, "grok_model_calls": model_calls}

    parsed_grok = _map_files(
        lambda path: _cached_file(path, "grok", cache_dir, parse_grok), grok_files, worker_cap
    )
    for payload in parsed_grok:
        if not payload:
            continue
        requests = payload["records"]
        costs = payload["grok_cost_ticks"]
        model_calls = payload["grok_model_calls"]
        by_turn: dict[tuple[str, str], list[dict]] = {}
        for request in requests:
            session_id, prompt_id, _model = request["key"].split("|", 2)
            by_turn.setdefault((session_id, prompt_id), []).append(request)
        for turn, turn_requests in by_turn.items():
            if turn in seen_grok_turns:
                continue
            seen_grok_turns.add(turn)
            for request in turn_requests:
                request["machine"] = machine
                grok_requests.append(request)
                if request["key"] in costs:
                    grok_cost_ticks[request["key"]] = costs[request["key"]]
                grok_model_calls[request["key"]] = model_calls.get(request["key"], 1)

    _prune_file_cache(cache_dir, active_cache)
    records = list(claude_requests.values()) + codex_requests + grok_requests
    records.sort(key=lambda request: (request["ts"] or 0, request["tool"], request["key"]))
    return Collection(
        records=records,
        grok_cost_ticks=dict(sorted(grok_cost_ticks.items())),
        grok_model_calls=dict(sorted(grok_model_calls.items())),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = collect()
    except ValueError as exc:
        parser.error(str(exc))
    json.dump(result.payload(), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
