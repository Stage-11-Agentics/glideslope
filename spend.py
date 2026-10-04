#!/usr/bin/env python3
"""Price local Claude Code, Codex, and Grok requests for Glideslope's spend view."""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import claude_account
import glideslope
import pricing
import spend_collect


HORIZONS = ("d1", "d7", "d30", "d90", "all")
MODEL_HORIZONS = ("d1", "d7", "d30", "all")
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write")
SPEEDS = ("standard", "flex", "fast", "ultrafast", "unknown")
PREMIUM_SPEEDS = ("fast", "ultrafast")
MAX_LOGIN_AGE_MS = 24 * 60 * 60 * 1000
WEEK_MS = 7 * 24 * 60 * 60 * 1000
DAILY_DAYS = 60
GROK_TICKS_PER_USD = 10_000_000_000
_PLAN_PRICE = {"max 20x": 200.0, "max 5x": 100.0, "pro": 20.0, "20x": 200.0}


def _log(message: str) -> None:
    sys.stderr.write("[glideslope spend] %s\n" % message)


def _iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def _roll_reset(end_ms: int, length_ms: int, now_ms: int) -> tuple[int, bool]:
    if end_ms > now_ms or length_ms <= 0:
        return end_ms, False
    steps = (now_ms - end_ms) // length_ms + 1
    return end_ms + steps * length_ms, True


def _account_names() -> list[str]:
    return list(dict.fromkeys(name for name in _alias_names().values() if name))


def _alias_names() -> dict[str, str]:
    aliases: dict[str, str] = {}
    roster = glideslope.read_roster(glideslope.CLAUDE_ROSTER)
    for alias, entry in roster.items():
        if not isinstance(alias, str) or not alias or not isinstance(entry, dict):
            continue
        email = entry.get("email")
        aliases[alias] = glideslope.call_sign(alias, email if isinstance(email, str) else None)
    for alias, name in tuple(glideslope.CLAUDE_CALL_SIGNS.items()):
        if alias and name and "@" not in str(alias):
            aliases.setdefault(str(alias), glideslope.call_sign(str(alias)))
    return aliases


def _read_sample_state(
    db: Path, now_ms: int, switch_log: Path | None = None
) -> tuple[dict, dict, dict, dict, dict]:
    """Read active login observations, switch events, and each account's latest meter sample."""
    aliases = _alias_names()
    timeline: list[tuple[int, str]] = []
    latest_meters: list[tuple] = []
    switch_path = Path(switch_log) if switch_log is not None else claude_account.SWITCH_LOG
    try:
        with switch_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                ms = _iso_ms(record.get("at")) if isinstance(record, dict) else None
                alias = record.get("to") if isinstance(record, dict) else None
                if ms is None or not isinstance(alias, str) or not alias:
                    continue
                name = aliases.get(alias)
                if name is None:
                    name = glideslope.call_sign(alias)
                    aliases[alias] = name
                timeline.append((ms, name))
    except OSError:
        pass

    if db.exists():
        try:
            uri = db.resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                columns = {row[1] for row in con.execute("PRAGMA table_info(samples)")}
                if columns:
                    plan_column = "s.plan" if "plan" in columns else "NULL"
                    active_column = "active" if "active" in columns else "0"

                    rows = con.execute(
                        "SELECT observed_at, account FROM samples"
                        " WHERE provider = 'Claude' AND meter = 'weekly_all' AND " + active_column + " = 1"
                        " ORDER BY observed_at"
                    )
                    for observed_at, alias in rows:
                        ms = _iso_ms(observed_at)
                        if ms is None:
                            continue
                        alias = str(alias)
                        name = aliases.get(alias)
                        if name is None:
                            name = glideslope.call_sign(alias)
                            aliases[alias] = name
                        timeline.append((ms, name))

                    # Use the latest complete row for each provider/account/meter without loading history.
                    latest_meters = con.execute(
                        "SELECT s.provider, s.account, s.meter, s.used_percent, s.window_minutes,"
                        " s.resets_at, s.observed_at, " + plan_column + " AS plan"
                        " FROM samples AS s JOIN ("
                        "   SELECT provider, account, meter, MAX(observed_at) AS observed_at"
                        "   FROM samples GROUP BY provider, account, meter"
                        " ) AS latest USING (provider, account, meter, observed_at)"
                        " WHERE s.provider IN ('Claude', 'Codex', 'Grok')"
                    ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            _log("sample database unavailable; using switch-log evidence for Claude attribution")

    timeline.sort(key=lambda point: point[0])
    names = [name for _, name in timeline]
    stamps = [stamp for stamp, _ in timeline]
    login_timeline = {"local": (stamps, names)} if stamps else {}
    earliest_evidence = stamps[0] if stamps else None
    attribution = {glideslope.LOCAL_SATELLITE: _iso(earliest_evidence)}

    account_plans: dict[str, str] = {}
    meters: dict[str, dict[str, Any]] = {}
    windows: dict[str, dict[str, Any]] = {}
    for provider, alias, meter, pct, minutes, resets_at, observed_at, plan in latest_meters:
        display = aliases.get(str(alias)) if provider == "Claude" else provider
        if not display:
            continue
        if isinstance(plan, str) and plan:
            account_plans[display] = plan
        if provider != "Claude" or meter not in ("weekly_all", "weekly_fable"):
            continue
        end_ms = _iso_ms(resets_at)
        observed_ms = _iso_ms(observed_at)
        if end_ms is None or pct is None:
            continue
        end_ms, rolled = _roll_reset(end_ms, WEEK_MS, now_ms)
        entry = windows.setdefault(display, {})
        entry[meter] = 0.0 if rolled else float(pct)
        if meter == "weekly_all":
            entry.update({
                "start_ms": end_ms - WEEK_MS,
                "end_ms": end_ms,
                "rolled": rolled,
                "observed_at": observed_at,
                "observed_ms": observed_ms,
            })

    meter_windows: dict[str, dict[str, Any]] = {}
    for provider, alias, meter, _pct, minutes, resets_at, _observed_at, _plan in latest_meters:
        display = aliases.get(str(alias)) if provider == "Claude" else provider
        if (not display or provider not in ("Claude", "Codex", "Grok")
                or not isinstance(minutes, int) or minutes <= 0):
            continue
        end_ms = _iso_ms(resets_at)
        if end_ms is None:
            continue
        length_ms = int(minutes) * 60_000
        end_ms, _rolled = _roll_reset(end_ms, length_ms, now_ms)
        meter_windows[f"{display}/{meter}"] = {
            "payer": display,
            "fable_only": meter == "weekly_fable",
            "start_ms": end_ms - length_ms,
            "end_ms": end_ms,
        }

    return login_timeline, attribution, account_plans, windows, meter_windows


def _account_at(timeline: tuple[list[int], list[str]] | None, ms: int | None) -> str:
    if not timeline or ms is None:
        return "unattributed"
    stamps, names = timeline
    index = bisect.bisect_right(stamps, ms) - 1
    if index < 0 or ms - stamps[index] > MAX_LOGIN_AGE_MS:
        return "unattributed"
    return names[index]


def _family(model: str | None) -> str:
    value = (model or "").lower()
    for name in ("fable", "opus", "sonnet", "haiku"):
        if name in value:
            return name
    if value.startswith(("gpt", "codex", "o1", "o3", "o4")):
        return "gpt"
    if "grok" in value:
        return "grok"
    return "other"


class Bucket:
    """A spend and token bucket with a complete speed breakdown."""

    def __init__(self) -> None:
        self.usd = 0.0
        self.tokens = 0
        self.fable_usd = 0.0
        self.fable_tokens = 0
        self.requests = 0
        self.split = [0, 0, 0, 0]
        self.reasoning = 0
        self.speeds: dict[str, list] = {}

    def add(self, usd: float, split: tuple[int, int, int, int], family: str,
            reasoning: int, calls: int, speed: str, standard_usd: float) -> None:
        tokens = sum(split)
        cell = self.speeds.setdefault(speed, [0.0, 0, 0, 0.0])
        cell[0] += usd
        cell[1] += tokens
        cell[2] += calls
        cell[3] += standard_usd
        self.usd += usd
        self.tokens += tokens
        self.requests += calls
        self.reasoning += reasoning
        for index, value in enumerate(split):
            self.split[index] += value
        if family == "fable":
            self.fable_usd += usd
            self.fable_tokens += tokens

    def merge(self, other: "Bucket") -> None:
        self.usd += other.usd
        self.tokens += other.tokens
        self.fable_usd += other.fable_usd
        self.fable_tokens += other.fable_tokens
        self.requests += other.requests
        self.reasoning += other.reasoning
        for index, value in enumerate(other.split):
            self.split[index] += value
        for speed, values in other.speeds.items():
            cell = self.speeds.setdefault(speed, [0.0, 0, 0, 0.0])
            for index, value in enumerate(values):
                cell[index] += value

    def dump(self) -> dict[str, Any]:
        speed = {
            name: {
                "usd": round(values[0], 2),
                "tokens": values[1],
                "requests": values[2],
                "premium_usd": round(values[0] - values[3], 2),
            }
            for name, values in sorted(
                self.speeds.items(), key=lambda item: SPEEDS.index(item[0]) if item[0] in SPEEDS else 9
            )
        }
        premium = sum(values[0] - values[3] for values in self.speeds.values())
        return {
            "usd": round(self.usd, 2),
            "tokens": self.tokens,
            "fable_usd": round(self.fable_usd, 2),
            "fable_tokens": self.fable_tokens,
            "requests": self.requests,
            **dict(zip(TOKEN_KINDS, self.split)),
            "reasoning": self.reasoning,
            "premium_usd": round(premium, 2),
            "speed": speed,
        }


def _plan_monthly(plan: str | None) -> float | None:
    if not plan:
        return None
    match = re.search(r"\$\s*(\d+(?:\.\d+)?)", plan)
    if match:
        return float(match.group(1))
    return _PLAN_PRICE.get(plan.strip().lower())


def _fast_mode_armed() -> dict[str, bool]:
    """Return only the armed state; never put local settings paths in spend.json."""
    armed: dict[str, bool] = {}
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        if settings.get("fastMode") is True:
            armed["claude"] = True
    except (OSError, ValueError, AttributeError):
        pass
    try:
        config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
        tier = config.get("service_tier")
        if tier and tier not in ("default", "auto", "flex"):
            armed["codex"] = True
    except (OSError, ValueError):
        pass
    return armed


def _pricing_meta(pricer: pricing.Pricer, evidence: collections.Counter,
                  armed: dict[str, bool]) -> dict[str, Any]:
    age = pricing.prices_age_h()
    pricer.report()
    if age is None or age > 72:
        _log("price table is missing or older than 72 hours; totals may be a floor")
    if armed:
        _log("premium speed is armed by default")
    return {
        "litellm_age_h": round(age, 1) if age is not None else None,
        "unpriced_tokens": dict(pricer.unpriced.most_common(10)),
        "underpriced_tokens": {f"{model}@{speed}": count
                                for (model, speed), count in pricer.underpriced.most_common(10)},
        "derived_tokens": {f"{model}@{speed}": count
                           for (model, speed), count in pricer.derived.most_common(10)},
        "estimated_tokens": {f"{model}@{speed}": count
                             for (model, speed), count in pricer.estimated.most_common(10)},
        "speed_evidence": {f"{tool}/{speed}/{source}": count
                           for (tool, speed, source), count in sorted(evidence.items())},
        "fast_mode_armed": armed,
    }


def build(collection: dict, now: dt.datetime | None = None, db: Path | None = None,
          pricer: pricing.Pricer | None = None) -> dict[str, Any]:
    """Build the complete spend contract from a minimal collector payload."""
    now = now or dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    db = Path(db) if db is not None else Path(glideslope.STORE_DIR) / "samples.db"
    pricer = pricer or pricing.Pricer()
    timelines, attribution, plans, windows, meter_windows = _read_sample_state(db, now_ms)
    cutoffs = {
        "d1": now_ms - 24 * 60 * 60 * 1000,
        "d7": now_ms - 7 * 24 * 60 * 60 * 1000,
        "d30": now_ms - 30 * 24 * 60 * 60 * 1000,
        "d90": now_ms - 90 * 24 * 60 * 60 * 1000,
    }

    names = _account_names()
    payers = names + ["Codex", "Grok", "unattributed"]
    totals = collections.defaultdict(Bucket)
    by_payer = collections.defaultdict(Bucket)
    by_model = collections.defaultdict(Bucket)
    by_machine = collections.defaultdict(Bucket)
    by_model_id = collections.defaultdict(Bucket)
    window_b = collections.defaultdict(Bucket)
    window_observed_b = collections.defaultdict(Bucket)
    meter_b = collections.defaultdict(Bucket)
    meters_by_payer: dict[str, list[tuple[str, dict]]] = collections.defaultdict(list)
    for key, meter in meter_windows.items():
        meters_by_payer[meter["payer"]].append((key, meter))

    local_tz = glideslope.LOCAL_TZ
    today = now.astimezone(local_tz).date()
    first_day = today - dt.timedelta(days=DAILY_DAYS - 1)
    daily = collections.defaultdict(lambda: collections.defaultdict(float))
    first_ms = None
    machines = set(collection.get("collected_at") or {})
    evidence: collections.Counter = collections.Counter()
    records = collection.get("records") if isinstance(collection, dict) else None
    records = records if isinstance(records, list) else []
    grok_costs = collection.get("grok_cost_ticks") or {}
    grok_calls = collection.get("grok_model_calls") or {}
    model_priced: dict[tuple[str, str], bool] = {}

    for record in records:
        if not isinstance(record, (dict, spend_collect.RequestRecord)):
            continue
        tool = record.get("tool")
        provider = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}.get(tool)
        if provider is None:
            continue
        model = str(record.get("model") or "unknown")
        ms = record.get("ts")
        try:
            ms = int(ms) if ms is not None else None
        except (TypeError, ValueError, OverflowError):
            ms = None
        if ms is not None:
            first_ms = ms if first_ms is None else min(first_ms, ms)
        split = tuple(max(0, int(record.get(key) or 0)) for key in ("in", "out", "cr", "cw5", "cw1"))
        token_split = (split[0], split[1], split[2], split[3] + split[4])
        tokens = sum(split)
        calls = max(1, int(grok_calls.get(record.get("key"), 1))) if tool == "grok" else 1
        speed = record.get("speed") or ("standard" if tool == "grok" else "unknown")
        if speed not in SPEEDS:
            speed = "unknown"
        evidence[(tool, speed, record.get("speed_src") or "-")] += calls
        family = _family(model)
        model_provider = provider

        if tool == "grok":
            key = record.get("key")
            if key not in grok_costs:
                priced = False
                usd = 0.0
            else:
                priced = True
                usd = int(grok_costs[key]) / GROK_TICKS_PER_USD
            standard_usd = usd
        else:
            cost = pricer.cost(
                model,
                split[0], split[1], split[2], split[3], split[4], tokens,
                speed=speed, prompt=record.get("prompt"),
            )
            priced = cost is not None
            usd = sum(cost) / 1_000_000 if cost is not None else 0.0
            if priced and speed in PREMIUM_SPEEDS:
                standard = pricer.cost(
                    model, split[0], split[1], split[2], split[3], split[4], 0,
                    speed=None, prompt=record.get("prompt"),
                )
                standard_usd = sum(standard) / 1_000_000 if standard is not None else usd
            else:
                standard_usd = usd
        model_key = (model_provider, model)
        model_priced[model_key] = model_priced.get(model_key, True) and priced
        args = (usd, token_split, family, max(0, int(record.get("think") or 0)), calls,
                speed, standard_usd)

        if provider == "Claude":
            payer = _account_at(timelines.get("local"), ms)
        else:
            payer = provider
        machine = str(record.get("machine") or glideslope.LOCAL_SATELLITE)
        machines.add(machine)

        if not priced:
            # Token rows retain unpriced models; dollar buckets omit them.
            model_spans = ["all"]
            if ms is not None:
                model_spans += [h for h in MODEL_HORIZONS[:-1] if ms >= cutoffs[h]]
            for horizon in model_spans:
                by_model_id[(model_provider, model, horizon)].add(*args)
            continue
        spans = ["all"] + ([h for h, cutoff in cutoffs.items() if ms is not None and ms >= cutoff])
        for horizon in spans:
            totals[horizon].add(*args)
            by_payer[(payer, horizon)].add(*args)
            by_model[(payer, family, horizon)].add(*args)
            by_machine[(machine, horizon)].add(*args)
            if horizon in MODEL_HORIZONS:
                by_model_id[(model_provider, model, horizon)].add(*args)

        if ms is not None:
            window = windows.get(payer)
            if window and window.get("start_ms") is not None and window["start_ms"] <= ms < window["end_ms"]:
                window_b[payer].add(*args)
                if window.get("rolled") or ms <= (window.get("observed_ms") or 0):
                    window_observed_b[payer].add(*args)
            for key, meter in meters_by_payer.get(payer, ()):
                if meter["start_ms"] <= ms < meter["end_ms"] and (
                    not meter["fable_only"] or family == "fable"
                ):
                    meter_b[key].add(*args)
            day = dt.datetime.fromtimestamp(ms / 1000, local_tz).date()
            if day >= first_day:
                series = payer if payer in payers else "unattributed"
                daily[day.isoformat()][series + (":fable" if family == "fable" else "")] += usd

    accounts = []
    for name in payers:
        provider = name if name in ("Codex", "Grok") else "Claude"
        entry: dict[str, Any] = {"name": name, "provider": provider}
        for horizon in HORIZONS:
            entry[horizon] = by_payer[(name, horizon)].dump()
        entry["models"] = {
            family: by_model[(name, family, "all")].dump()
            for family in ("fable", "opus", "sonnet", "haiku", "gpt", "grok", "other")
            if by_model[(name, family, "all")].requests
        }
        window = windows.get(name)
        if window and window.get("start_ms") is not None:
            bucket = window_b[name].dump()
            observed = window_observed_b[name]
            pct_all = window.get("weekly_all") or 0
            pct_fable = window.get("weekly_fable") or 0
            entry["window"] = {
                "start": _iso(window["start_ms"]),
                "end": _iso(window["end_ms"]),
                "weekly_all_pct": window.get("weekly_all"),
                "weekly_fable_pct": window.get("weekly_fable"),
                **bucket,
                "usd_per_pct_all": round(observed.usd / pct_all, 2) if pct_all >= 5 else None,
                "usd_per_pct_fable": round(observed.fable_usd / pct_fable, 2) if pct_fable >= 5 else None,
                "pct_observed_at": window.get("observed_at"),
                "pct_presumed": bool(window.get("rolled")),
                "pct_stale": bool(
                    window.get("observed_ms") and not window.get("rolled")
                    and now_ms - window["observed_ms"] > 6 * 60 * 60 * 1000
                ),
            }
        plan = plans.get(name)
        if plan is None:
            plan = glideslope.CODEX_PLAN if name == "Codex" else (
                glideslope.GROK_PLAN if name == "Grok" else glideslope.CLAUDE_PLAN
            )
        entry["plan_tier"] = plan
        accounts.append(entry)

    claude_monthly = sum(
        _plan_monthly(plans.get(name, glideslope.CLAUDE_PLAN)) or 0.0 for name in names
    )
    codex_monthly = _plan_monthly(plans.get("Codex", glideslope.CODEX_PLAN))
    claude_d30 = sum(by_payer[(name, "d30")].usd for name in names + ["unattributed"])
    codex_d30 = by_payer[("Codex", "d30")].usd
    leverage = {
        "claude_subscriptions_monthly_usd": round(claude_monthly, 2),
        "claude_api_equivalent_d30_usd": round(claude_d30, 2),
        "claude_multiple": round(claude_d30 / claude_monthly, 1) if claude_monthly else None,
        "codex_subscription_monthly_usd": codex_monthly,
        "codex_api_equivalent_d30_usd": round(codex_d30, 2),
        "codex_multiple": round(codex_d30 / codex_monthly, 1) if codex_monthly else None,
    }

    providers: dict[str, dict] = {}
    for group, members in (("Anthropic", names + ["unattributed"]), ("Codex", ["Codex"]),
                           ("Grok", ["Grok"])):
        providers[group] = {}
        for horizon in HORIZONS:
            bucket = Bucket()
            for member in members:
                bucket.merge(by_payer[(member, horizon)])
            providers[group][horizon] = bucket.dump()
    providers["Total"] = {horizon: totals[horizon].dump() for horizon in HORIZONS}

    meter_view = {
        key: {
            **meter_b[key].dump(),
            "start": _iso(meter["start_ms"]),
            "end": _iso(meter["end_ms"]),
            "fable_only": meter["fable_only"],
        }
        for key, meter in meter_windows.items()
    }
    model_keys = sorted(
        {(provider, model) for provider, model, _horizon in by_model_id},
        key=lambda key: (-by_model_id[(*key, "all")].tokens, key),
    )
    model_tokens = [
        {
            "provider": provider,
            "model": model,
            "priced": model_priced.get((provider, model), False),
            **{horizon: by_model_id[(provider, model, horizon)].dump() for horizon in MODEL_HORIZONS},
        }
        for provider, model in model_keys
    ]
    days = [(today - dt.timedelta(days=offset)).isoformat() for offset in range(DAILY_DAYS - 1, -1, -1)]
    armed = _fast_mode_armed()
    return {
        "generated_at": now.isoformat(),
        "collected_at": collection.get("collected_at") or {},
        "first_request_at": _iso(first_ms),
        "attribution": attribution,
        "totals": {horizon: totals[horizon].dump() for horizon in HORIZONS},
        "machines": {
            machine: {horizon: by_machine[(machine, horizon)].dump() for horizon in ("d7", "d30", "all")}
            for machine in sorted(machines)
        },
        "accounts": accounts,
        "leverage": leverage,
        "providers": providers,
        "meters": meter_view,
        "daily": {
            "days": days,
            "series": {
                key: [round(daily.get(day, {}).get(key, 0.0), 2) for day in days]
                for key in sorted({series for by_day in daily.values() for series in by_day})
            },
        },
        "model_tokens": model_tokens,
        "pricing": {
            **_pricing_meta(pricer, evidence, armed),
        },
    }


def _atomic_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix="." + path.name + ".", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _load_saved_collection(store: Path) -> dict | None:
    """Load last success metadata and rebuild its request collection from per-file cache."""
    metadata_path = store / "spend" / "collection-state.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    collected_at = metadata.get("collected_at") if isinstance(metadata, dict) else None
    if not isinstance(collected_at, dict) or not collected_at:
        return None
    collection = spend_collect.load_cached_collection(machine=glideslope.LOCAL_SATELLITE)
    if collection is None:
        return None
    payload = collection.internal_payload()
    payload["collected_at"] = collected_at
    return payload


def summary(data: dict) -> str:
    totals = data["totals"]
    lines = [
        "API-equivalent spend (list prices; subscriptions are what was paid)",
        "  24h ${:,.2f} · 7d ${:,.2f} · 30d ${:,.2f} · all ${:,.2f}".format(
            totals["d1"]["usd"], totals["d7"]["usd"], totals["d30"]["usd"], totals["all"]["usd"]
        ),
    ]
    for account in data["accounts"]:
        lines.append("  {:<16} 7d ${:,.2f} · 30d ${:,.2f} · all ${:,.2f}".format(
            account["name"], account["d7"]["usd"], account["d30"]["usd"], account["all"]["usd"]
        ))
    return "\n".join(lines)


def _produce(no_collect: bool = False, print_summary: bool = False) -> int:
    store = Path(glideslope.STORE_DIR)
    collection_state = store / "spend" / "collection-state.json"
    now = dt.datetime.now(dt.timezone.utc)
    if no_collect:
        collection = _load_saved_collection(store)
        if collection is None:
            _log("no saved collection is available")
            return 2
    else:
        try:
            result = spend_collect.collect(machine=glideslope.LOCAL_SATELLITE)
            collection = result.internal_payload()
            collection["collected_at"] = {glideslope.LOCAL_SATELLITE: now.isoformat()}
            _atomic_json({"collected_at": collection["collected_at"]}, collection_state)
        except Exception:  # noqa: BLE001 — a failed collection cannot corrupt the last good spend file
            _log("request collection failed; keeping the last saved per-file cache")
            collection = _load_saved_collection(store)
            if collection is None:
                return 1

    pricing.refresh_prices()
    try:
        data = build(collection, now=now)
        _atomic_json(data, store / "spend.json")
    except Exception:  # noqa: BLE001 — spend is an optional view input
        _log("producer failed; the prior spend file remains in place")
        return 1
    _log("spend file updated")
    if print_summary:
        print(summary(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print", action="store_true", help="print a compact spend summary")
    parser.add_argument("--no-collect", action="store_true", help="price the last saved collection")
    parser.add_argument("command", nargs="?", choices=("prices",), help="prices: show rates in force")
    parser.add_argument("--refresh", action="store_true", help="refresh prices before listing them")
    args = parser.parse_args(argv)
    if args.command == "prices":
        if args.print or args.no_collect:
            parser.error("--print and --no-collect apply only to spend production")
        return pricing.prices_cli(force=args.refresh)
    if args.refresh:
        parser.error("--refresh is only valid with the prices subcommand")
    store = Path(glideslope.STORE_DIR)
    try:
        store.mkdir(parents=True, exist_ok=True)
        lock = (store / ".spend-producer.lock").open("a+")
    except OSError:
        _log("producer lock unavailable")
        return 1
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log("producer already running")
            return 0
        return _produce(no_collect=args.no_collect, print_summary=args.print)
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
