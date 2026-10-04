#!/usr/bin/env python3
"""Price request token counts at API list rates and retain speed-tier evidence."""

from __future__ import annotations

import collections
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sys
import time
import tomllib
import urllib.request
from pathlib import Path


# Per 1M tokens: input, output, cache read, cache write 5m, cache write 1h.
# These Claude rates reproduce Claude Code's cost-state records.
CLAUDE_PRICES = {
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20),
    "claude-fable-5": (10, 50, 1.0, 12.5, 20),
    "claude-opus-5-5": (4, 20, 0.2, 5, 8),
    "claude-opus-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-8": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-7": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-6": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-5": (5, 25, 0.5, 6.25, 10),
    "claude-sonnet-5": (2, 10, 0.2, 2.5, 4),
    "claude-sonnet-4-6": (3, 15, 0.3, 3.75, 6),
    "claude-sonnet-4-5": (3, 15, 0.3, 3.75, 6),
    "claude-haiku-4-5": (1, 5, 0.1, 1.25, 2),
}

# Opus 5.5 fast mode is 2x standard. Other Claude fast rates remain underpriced
# until independently verified.
CLAUDE_SPEED_MULT = {"fast": {"claude-opus-5-5": 2.0}}
PREMIUM_SPEEDS = ("fast", "ultrafast")
LITELLM_TIER = {"fast": "priority", "ultrafast": "ultrafast", "flex": "flex"}
TIERED_SPEEDS = tuple(LITELLM_TIER)
ESTIMATED_TIER_MULT = {"gpt-6.1-sol": {"ultrafast": 6.0}}
LONG_CONTEXT = 272000
CODEX_SPEED = {
    "default": "standard", "auto": "standard", "flex": "flex",
    "priority": "fast", "fast": "fast", "ultrafast": "ultrafast",
}

PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICES_MAX_AGE_H = 24
RATE_FIELDS = re.compile(r"(cost_per_token|token_cost)")


def _store_dir() -> Path:
    """Resolve the configured store only when a pricing operation needs it."""
    override = os.environ.get("GLIDESLOPE_HOME")
    if override:
        return Path(os.path.expanduser(override))

    config_path = Path(os.path.expanduser(
        os.environ.get("GLIDESLOPE_CONFIG") or str(Path.home() / ".glideslope" / "config.toml")
    ))
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ValueError):
        config = {}
    store = config.get("store") if isinstance(config, dict) else None
    return Path(os.path.expanduser(str(store))) if store else Path.home() / ".glideslope"


def _paths() -> dict[str, Path]:
    root = _store_dir() / "pricing"
    return {
        "root": root,
        "prices": root / "prices-litellm.json",
        "meta": root / "prices-litellm.meta.json",
        "changes": root / "prices-changes.jsonl",
        "speed_ledger": root / "speed-ledger.json",
    }


def _log(message: str) -> None:
    sys.stderr.write("[glideslope pricing] %s\n" % message)


def _read_table(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _lite_rate(v: dict, field: str, long: bool, tier: str | None) -> tuple[float | None, bool]:
    """Return one LiteLLM rate and whether its premium rate had to be derived."""
    long_suffix = "_above_272k_tokens" if long and v.get("input_cost_per_token_above_272k_tokens") is not None else ""
    tier_suffix = "_" + tier if tier else ""
    value = v.get(field + long_suffix + tier_suffix)
    if value is not None:
        return value, False
    base = v.get(field + long_suffix)
    if base is None or not tier:
        return base, False
    for suffix in (long_suffix, ""):
        tier_input = v.get("input_cost_per_token" + suffix + tier_suffix)
        base_input = v.get("input_cost_per_token" + suffix)
        if tier_input and base_input:
            return base * tier_input / base_input, True
    return None, False


class Pricer:
    """Calculate per-request API-equivalent cost products by token kind.

    ``lite`` can be injected for deterministic offline use. ``extra_prices`` is
    an optional caller-supplied mapping and defaults to no additional rates.
    """

    def __init__(self, lite: dict | None = None, extra_prices: dict | None = None):
        self.lite = lite if lite is not None else _read_table(_paths()["prices"])
        self.extra_prices = dict(extra_prices or {})
        self.cache = {}
        self.unpriced = collections.Counter()
        self.underpriced = collections.Counter()
        self.derived = collections.Counter()
        self.estimated = collections.Counter()

    def _lite_entry(self, base: str) -> dict | None:
        candidates = [base]
        match = re.match(r"^(gpt-[\d.]+)", base)
        if match:
            candidates.append(match.group(1))
        if base.startswith("gpt-5") and base.endswith("-codex"):
            candidates.append(base[:-6])
        candidates.append("gpt-5" if base.startswith(("gpt-5", "codex")) else None)
        for candidate in candidates:
            value = self.lite.get(candidate) if candidate else None
            if value and value.get("input_cost_per_token") is not None:
                return value
        return None

    def rates(self, model: str | None, speed: str | None = None, prompt: int = 0):
        """Return (five per-1M rates, status) for a model, speed, and prompt size."""
        premium = speed if speed in TIERED_SPEEDS else None
        long = prompt > LONG_CONTEXT
        key = (model, premium, long)
        if key in self.cache:
            return self.cache[key]

        rates, status = None, None
        base = (model or "").replace("[1m]", "")
        for name, values in CLAUDE_PRICES.items():
            if base == name or base.startswith(name + "-"):
                multiplier = CLAUDE_SPEED_MULT.get(premium, {}).get(name) if premium else 1.0
                if multiplier is None:
                    rates, status = values, "standard"
                else:
                    rates, status = tuple(value * multiplier for value in values), "list"
                break

        if rates is None and base in self.extra_prices:
            rates = self.extra_prices[base]
            status = "standard" if premium else "list"

        if rates is None:
            value = self._lite_entry(base)
            if value:
                tier = LITELLM_TIER.get(premium)
                estimate = ESTIMATED_TIER_MULT.get(base, {}).get(premium)
                if tier and value.get("input_cost_per_token_" + tier) is None:
                    tier, status = None, ("estimated" if estimate else "standard")
                fields = (
                    "input_cost_per_token", "output_cost_per_token",
                    "cache_read_input_token_cost", "cache_creation_input_token_cost",
                )
                got = [_lite_rate(value, field, long, tier) for field in fields]
                if tier and (got[0][0] is None or got[1][0] is None):
                    tier, status = None, "standard"
                    got = [_lite_rate(value, field, long, None) for field in fields]
                derived = any(is_derived for _, is_derived in got)
                values = [rate for rate, _ in got]
                if status == "estimated":
                    values = [rate * estimate if rate is not None else None for rate in values]
                input_rate = (values[0] or 0) * 1e6
                output_rate = (values[1] or 0) * 1e6
                cache_read_rate = (values[2] or 0) * 1e6
                cache_write_rate = values[3] * 1e6 if values[3] is not None else input_rate
                rates = (input_rate, output_rate, cache_read_rate, cache_write_rate, cache_write_rate)
                status = status or ("derived" if derived else "list")

        self.cache[key] = (rates, status)
        return rates, status

    def cost(self, model, inp, out, cr, cw5, cw1, tokens, speed=None, prompt=None):
        """Return input/output/cache cost products; prompt is this request's prompt size."""
        if prompt is None:
            prompt = inp + cr + cw5 + cw1
        rates, status = self.rates(model, speed, prompt)
        if rates is None:
            self.unpriced[model] += tokens
            return None
        if status == "standard":
            self.underpriced[(model, speed)] += tokens
        elif status == "derived":
            self.derived[(model, speed)] += tokens
        elif status == "estimated":
            self.estimated[(model, speed)] += tokens
        return (
            inp * rates[0], out * rates[1], cr * rates[2],
            cw5 * rates[3] + cw1 * rates[4],
        )

    def report(self) -> None:
        """Log model gaps and premium-speed rates that need follow-up."""
        if self.unpriced:
            _log("UNPRICED models (tokens, cost left out): %s" % dict(self.unpriced.most_common(10)))
        if self.underpriced:
            _log("UNDERPRICED premium speed, priced at standard (tokens): %s"
                 % {"%s@%s" % key: value for key, value in self.underpriced.most_common(10)})
        if self.estimated:
            _log("ESTIMATED premium rate, no published price (tokens): %s"
                 % {"%s@%s" % key: value for key, value in self.estimated.most_common(10)})
        if self.derived:
            _log("premium cache rates derived from the input ratio (tokens): %s"
                 % {"%s@%s" % key: value for key, value in self.derived.most_common(10)})


def prices_age_h(path: str | Path | None = None) -> float | None:
    """Hours since the cached price table was fetched, or None when it is absent."""
    price_path = Path(path) if path is not None else _paths()["prices"]
    return (time.time() - price_path.stat().st_mtime) / 3600 if price_path.exists() else None


class _Lock:
    """An exclusive flock stored alongside the pricing data."""

    def __init__(self, name: str, directory: str | Path | None = None):
        self.directory = Path(directory) if directory is not None else _paths()["root"]
        self.path = self.directory / (name + ".lock")
        self.fh = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def refresh_prices(force: bool = False, max_age_h: float = PRICES_MAX_AGE_H) -> str:
    paths = _paths()
    with _Lock("prices", directory=paths["root"]):
        return _refresh_prices(force, max_age_h, paths)


def _refresh_prices(force: bool, max_age_h: float, paths: dict[str, Path] | None = None) -> str:
    """Fetch the public price table when stale; keep the previous table on failure."""
    paths = paths or _paths()
    age = prices_age_h(paths["prices"])
    if not force and age is not None and age < max_age_h:
        return "fresh"
    try:
        with urllib.request.urlopen(PRICES_URL, timeout=30) as response:
            raw = response.read()
        new = json.loads(raw)
        if not isinstance(new, dict) or len(new) < 1000 or "gpt-5" not in new:
            raise ValueError("implausible table (%s entries)" % len(new) if isinstance(new, dict) else "not a dict")
    except Exception as exc:
        _log("price refresh FAILED (%s); keeping the table from %.0f h ago" % (exc, age or -1))
        return "failed"

    old = _read_table(paths["prices"])

    def rates_of(table: dict, model: str) -> dict:
        value = table.get(model) or {}
        return {field: rate for field, rate in value.items()
                if RATE_FIELDS.search(field) and isinstance(rate, (int, float))}

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    changes = []
    for model in sorted(set(old) | set(new)):
        if "/" in model or not model.startswith(("gpt-", "o1", "o3", "o4", "codex", "claude-")):
            continue
        before, after = rates_of(old, model), rates_of(new, model)
        if before == after:
            continue
        fields = {field: [before.get(field), after.get(field)]
                  for field in sorted(set(before) | set(after)) if before.get(field) != after.get(field)}
        changes.append({
            "at": now, "model": model,
            "change": "added" if not before else "removed" if not after else "changed",
            "fields": fields,
        })

    tmp = Path("%s.%d.tmp" % (paths["prices"], os.getpid()))
    with tmp.open("wb") as handle:
        handle.write(raw)
    if paths["prices"].exists():
        shutil.copyfile(paths["prices"], str(paths["prices"]) + ".prev")
    os.replace(tmp, paths["prices"])
    with paths["meta"].open("w", encoding="utf-8") as handle:
        json.dump({"fetched_at": now, "url": PRICES_URL, "models": len(new)}, handle)
    if changes:
        with paths["changes"].open("a", encoding="utf-8") as handle:
            for change in changes:
                handle.write(json.dumps(change) + "\n")
        kinds = collections.Counter(change["change"] for change in changes)
        _log("prices refreshed: %s (%s)" % (dict(kinds), ", ".join(c["model"] for c in changes[:12])))
    else:
        _log("prices refreshed: no first-party rate changed")
    for line in claude_drift(new):
        _log("CLAUDE_PRICES drift, re-verify: " + line)
    return "updated" if changes else "unchanged"


def claude_drift(lite: dict | None = None) -> list[str]:
    """Compare the hand-kept Claude table with LiteLLM; report differences, never rewrite."""
    if lite is None:
        lite = _read_table(_paths()["prices"])
    out = []
    fields = (
        "input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
    )
    for model, ours in CLAUDE_PRICES.items():
        value = lite.get(model)
        if not value:
            continue
        for field, mine in zip(fields, ours[:4]):
            theirs = value.get(field)
            if theirs is not None and abs(theirs * 1e6 - mine) > 1e-9:
                out.append("%s %s: ours %.4g, LiteLLM %.4g" % (model, field, mine, theirs * 1e6))
        for field in value:
            if "fast" in field and field != "supports_fast_mode":
                multiplier = CLAUDE_SPEED_MULT["fast"].get(model)
                out.append("%s: LiteLLM lists %s=%s (we use multiplier %s)" %
                           (model, field, value[field], multiplier))
    return out


class SpeedLedger:
    """Retain logged Codex service tiers after the source log's retention expires."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else _paths()["speed_ledger"]
        self.runs = {}
        self.seen = {}
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    self.runs = json.load(handle)
            except ValueError:
                _log("speed ledger unreadable; starting empty (the log still covers about ten days)")

    def lookup(self, session_key: str, index: int):
        for start, end, tier, source in self.runs.get(session_key, ()):
            if start <= index <= end:
                return tier, source
        return None

    def apply(self, request: dict) -> None:
        if request.get("tool") != "codex" or not request.get("key") or "|" not in request["key"]:
            return
        session, _, raw_index = request["key"].rpartition("|")
        try:
            index = int(raw_index)
        except ValueError:
            return
        session_key = "%s|%s" % (request.get("machine"), session)
        if request.get("speed_src") in ("log", "log-next"):
            if self.lookup(session_key, index) != (request.get("tier"), request["speed_src"]):
                self.seen.setdefault(session_key, {})[index] = [request.get("tier"), request["speed_src"]]
            return
        hit = self.lookup(session_key, index)
        if hit:
            request["tier"], request["speed_src"] = hit
            request["speed"] = CODEX_SPEED.get(hit[0], hit[0])

    def save(self) -> None:
        if not self.seen:
            return
        with _Lock("speed-ledger", directory=self.path.parent):
            if self.path.exists():
                try:
                    with self.path.open("r", encoding="utf-8") as handle:
                        self.runs = json.load(handle)
                except ValueError:
                    pass
            self._merge_and_write()

    def _merge_and_write(self) -> None:
        for session_key, new in self.seen.items():
            cells = {}
            for start, end, tier, source in self.runs.get(session_key, ()):
                for index in range(start, end + 1):
                    cells[index] = [tier, source]
            cells.update(new)
            runs = []
            for index in sorted(cells):
                tier, source = cells[index]
                if runs and runs[-1][1] == index - 1 and runs[-1][2] == tier and runs[-1][3] == source:
                    runs[-1][1] = index
                else:
                    runs.append([index, index, tier, source])
            self.runs[session_key] = runs
        self.seen = {}
        tmp = Path("%s.%d.tmp" % (self.path, os.getpid()))
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.runs, handle, separators=(",", ":"))
        os.replace(tmp, self.path)


def _first_party_model(model: str) -> bool:
    return "/" not in model and model.startswith(("gpt-", "o1", "o3", "o4", "codex", "claude-"))


def prices_cli(force: bool = False) -> int:
    if force:
        refresh_prices(force=True)
    paths = _paths()
    age = prices_age_h(paths["prices"])
    table = _read_table(paths["prices"])
    print("LiteLLM table: %s" % (
        "missing" if age is None else "%.1f h old (refreshes after %d h)" % (age, PRICES_MAX_AGE_H)
    ))
    for line in claude_drift(table):
        print("CLAUDE_PRICES drift, re-verify: " + line)

    pricer = Pricer(lite=table)
    models = set(CLAUDE_PRICES)
    models.update(
        model for model, value in table.items()
        if _first_party_model(model) and isinstance(value, dict) and value.get("input_cost_per_token") is not None
    )
    print("%-30s %-10s %-9s %8s %8s %8s %8s" %
          ("model", "speed", "status", "in", "out", "cache rd", "cache wr"))
    for model in sorted(models):
        for speed in ("standard", "fast", "ultrafast"):
            rates, status = pricer.rates(model, speed)
            if rates is None or (speed != "standard" and status == "standard"):
                continue
            print("%-30s %-10s %-9s %8.2f %8.2f %8.3f %8.2f" %
                  (model, speed, status, rates[0], rates[1], rates[2], rates[4]))
    print("per 1M tokens; cache write is the 1h rate. Premium rates without a published or estimated price are omitted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "prices" or any(arg != "--refresh" for arg in args[1:]):
        print("Usage: python3 pricing.py prices [--refresh]", file=sys.stderr)
        return 2
    return prices_cli(force="--refresh" in args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
