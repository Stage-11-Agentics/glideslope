#!/usr/bin/env python3
"""Build the local Spend view from spend.json and the read-only sample store.

    python3 views/spend-src/build.py
    python3 views/spend-src/build.py --spend fixture.json --samples fixture.db --out page.html

The page is a local valuation view. It never contacts a provider, reads a
credential, or writes to the sample store. Missing meter reads stay unknown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

SRC = Path(__file__).resolve().parent
VIEWS = SRC.parent
ROOT = VIEWS.parent
sys.path.insert(0, str(ROOT))
import glideslope  # noqa: E402 — config supplies only the store path and display settings

STORE = glideslope.STORE_DIR
SPEND_JSON = STORE / "spend.json"
SAMPLES_DB = STORE / "samples.db"
TEMPLATE = SRC / "spend.tmpl.html"
OUT = VIEWS / "spend.html"
ICON_SOURCE = VIEWS / "glideslope-icon.svg"
TOKENS = (
    "__SPEND_JSON__", "__METER_JSON__", "__BUILD_JSON__", "__GLIDESLOPE_ICON__",
    "__SPEND_EXTENSION_DATA__", "__SPEND_EXTENSION_HOOK__",
)
WINDOWS = ("d1", "d7", "d30", "d90", "all")
BUCKET_FIELDS = (
    "usd", "tokens", "input", "cache_read", "cache_write", "output", "reasoning",
    "requests", "fable_usd", "fable_tokens", "premium_usd",
)
SPEED_TIERS = ("standard", "flex", "fast", "ultrafast", "unknown")
SPEED_FIELDS = ("usd", "tokens", "requests", "premium_usd")
ACCOUNT_PROVIDERS = {"Claude", "Codex", "Grok", "Kimi", "OpenRouter"}
PUBLIC_PROVIDER_NAMES = {"Anthropic", "Codex", "Grok", "Grok Build", "Kimi", "OpenRouter", "Total"}
METER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
METER_IDS_BY_PROVIDER = {
    "Claude": {"session", "weekly_all", "weekly_fable"},
    "Codex": {"codex"},
    "Grok": {"session", "weekly_all"},
}
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$")
ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FALLBACK_ICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Cpath d='M8 1l7 7-7 7-7-7z' fill='%23e8eef6'/%3E%3C/svg%3E"
)


class BuildError(Exception):
    """The page could not be built; an existing page remains untouched."""


def warn(message: str) -> None:
    sys.stderr.write(f"[spend-page] {message}\n")


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def iso_z(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def codex_history_outside_percent(spend: dict[str, Any]) -> int | None:
    """Percent of Codex all-time spend absent from a complete dated 60-day series."""
    account = next((item for item in spend.get("accounts", [])
                    if isinstance(item, dict) and item.get("provider") == "Codex"), None)
    total = account.get("all", {}).get("usd") if account and isinstance(account.get("all"), dict) else None
    daily = spend.get("daily") if isinstance(spend.get("daily"), dict) else {}
    days = daily.get("days") if isinstance(daily.get("days"), list) else []
    series_in = daily.get("series") if isinstance(daily.get("series"), dict) else {}
    series = series_in.get(account.get("name")) if account else None
    if (not _number(total) or total <= 0 or not days or not isinstance(series, list)
            or len(series) != len(days) or not all(_number(value) for value in series)):
        return None
    outside = total - sum(series)
    if outside < 0:
        return None
    return round(100 * outside / total)


def codex_history_note(spend: dict[str, Any]) -> str | None:
    account = next((item for item in spend.get("accounts", [])
                    if isinstance(item, dict) and item.get("provider") == "Codex"), None)
    total = account.get("all", {}).get("usd") if account and isinstance(account.get("all"), dict) else None
    if not _number(total) or total <= 0:
        return None
    outside = codex_history_outside_percent(spend)
    return f"{outside}% outside the dated 60-day series" if outside is not None else "60-day history unknown"


def curve_account_names(spend: dict[str, Any]) -> list[str]:
    daily = spend.get("daily") if isinstance(spend.get("daily"), dict) else {}
    series = daily.get("series") if isinstance(daily.get("series"), dict) else {}

    def has_spend(values: Any) -> bool:
        return isinstance(values, list) and any(_number(value) and value > 0 for value in values)

    return [account["name"] for account in spend.get("accounts", [])
            if isinstance(account, dict) and isinstance(account.get("name"), str)
            and (has_spend(series.get(account["name"])) or has_spend(series.get(account["name"] + ":fable")))]


def _bucket(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = {key: raw[key] for key in BUCKET_FIELDS if _number(raw.get(key))}
    speed = raw.get("speed")
    if isinstance(speed, dict):
        result["speed"] = {
            tier: {key: values[key] for key in SPEED_FIELDS if _number(values.get(key))}
            for tier, values in speed.items()
            if tier in SPEED_TIERS and isinstance(values, dict)
        }
    return result


def _horizons(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: _bucket(raw.get(key)) for key in WINDOWS if isinstance(raw.get(key), dict)}


def _safe_account(raw: dict[str, Any]) -> dict[str, Any] | None:
    name, provider = raw.get("name"), raw.get("provider")
    if not isinstance(name, str) or not name.strip() or not isinstance(provider, str) or not provider.strip():
        return None
    # A display label is the only identity this page is allowed to carry.
    name = name.strip()
    if ("@" in name or "/" in name or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or provider not in ACCOUNT_PROVIDERS):
        return None
    account: dict[str, Any] = {"name": name, "provider": provider}
    tier = raw.get("plan_tier")
    if isinstance(tier, str) and tier:
        account["plan_tier"] = glideslope.CLAUDE_PLAN_BY_TIER.get(tier, tier)
    for horizon in WINDOWS:
        if isinstance(raw.get(horizon), dict):
            account[horizon] = _bucket(raw[horizon])
    models = raw.get("models")
    if isinstance(models, dict):
        account["models"] = {
            key: _bucket(value) for key, value in models.items()
            if isinstance(key, str) and MODEL_ID_RE.fullmatch(key)
        }
    window = raw.get("window")
    if isinstance(window, dict):
        fields = (
            "start", "end", "weekly_all_pct", "weekly_fable_pct", "usd", "tokens",
            "fable_usd", "fable_tokens", "usd_per_pct_all", "usd_per_pct_fable",
            "pct_observed_at", "pct_presumed", "pct_stale",
        )
        account["window"] = {
            key: window[key] for key in fields
            if key in window and (key.endswith("_at") or isinstance(window[key], (str, bool)) or _number(window[key]))
        }
    return account


def _safe_pricing(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    keys = (
        "litellm_age_h", "unpriced_tokens", "underpriced_tokens", "derived_tokens",
        "estimated_tokens", "fast_mode_armed",
    )
    result: dict[str, Any] = {}
    for key in keys:
        value = raw.get(key)
        if key == "litellm_age_h" and _number(value):
            result[key] = value
        elif key == "fast_mode_armed" and isinstance(value, dict):
            result[key] = {tool: armed for tool, armed in value.items()
                           if tool in {"claude", "codex"} and isinstance(armed, bool)}
        elif key in {"unpriced_tokens", "underpriced_tokens", "derived_tokens", "estimated_tokens"} and isinstance(value, dict):
            result[key] = {
                model: count for model, count in value.items()
                if isinstance(model, str) and MODEL_ID_RE.fullmatch(model) and _number(count)
            }
    return result


def safe_spend(raw: dict[str, Any], *, sampled_meter_keys: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Select render data; machine labels, aliases, attribution and unknown keys stay out."""
    accounts = [account for item in raw.get("accounts", [])
                if isinstance(item, dict) and (account := _safe_account(item)) is not None]
    accounts_by_name = {account["name"]: account for account in accounts}
    names = set(accounts_by_name)
    providers: dict[str, Any] = {}
    for provider, horizons in (raw.get("providers") or {}).items():
        if provider in PUBLIC_PROVIDER_NAMES and isinstance(horizons, dict):
            providers[provider] = _horizons(horizons)

    totals = _horizons(raw.get("totals"))
    if "all" not in totals:
        raise BuildError("spend.json is missing totals.all")

    daily = raw.get("daily") if isinstance(raw.get("daily"), dict) else {}
    days = daily.get("days") if isinstance(daily.get("days"), list) else []
    days = [day for day in days if isinstance(day, str) and ISO_DAY_RE.fullmatch(day)]
    series_in = daily.get("series") if isinstance(daily.get("series"), dict) else {}
    series: dict[str, list[float | None]] = {}
    for key, values in series_in.items():
        if not isinstance(key, str):
            continue
        name = key.removesuffix(":fable") if key.endswith(":fable") else key
        if name not in names or not isinstance(values, list) or len(values) != len(days):
            continue
        if not all(_number(value) for value in values):
            continue
        series[key] = [float(value) for value in values]

    leverage_in = raw.get("leverage") if isinstance(raw.get("leverage"), dict) else {}
    leverage = {key: value for key, value in leverage_in.items()
                if isinstance(key, str) and key in {
                    "claude_multiple", "codex_multiple", "grok_multiple",
                    "claude_api_equivalent_d30_usd", "codex_api_equivalent_d30_usd",
                    "grok_api_equivalent_d30_usd", "claude_subscriptions_monthly_usd",
                    "codex_subscription_monthly_usd", "grok_subscription_monthly_usd",
                }
                and (value is None or _number(value))}

    # The ledger meter map carries display/id but not provider. Keep known IDs in
    # standalone projections; a full build may add any ID proved by samples.db.
    meters_in = raw.get("meters") if isinstance(raw.get("meters"), dict) else {}
    meters: dict[str, Any] = {}
    for key, value in meters_in.items():
        display, sep, meter_id = key.partition("/") if isinstance(key, str) else ("", "", "")
        account = accounts_by_name.get(display)
        sampled_match = sampled_meter_keys is not None and (display, meter_id) in sampled_meter_keys
        known_id = account is not None and meter_id in METER_IDS_BY_PROVIDER.get(account["provider"], set())
        if (account is not None and sep and METER_ID_RE.fullmatch(meter_id)
                and (sampled_match or known_id) and isinstance(value, dict)):
            meters[key] = _bucket(value)
            for date_key in ("start", "end"):
                if isinstance(value.get(date_key), str):
                    meters[key][date_key] = value[date_key]

    result: dict[str, Any] = {
        "generated_at": raw.get("generated_at") if isinstance(raw.get("generated_at"), str) else None,
        "first_request_at": raw.get("first_request_at") if isinstance(raw.get("first_request_at"), str) else None,
        "totals": totals,
        "accounts": accounts,
        "providers": providers,
        "leverage": leverage,
        "pricing": _safe_pricing(raw.get("pricing")),
        "meters": meters,
        "daily": {"days": days, "series": series},
    }
    return result


def _load_spend_raw(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise BuildError("spend.json not found") from None
    except OSError as exc:
        raise BuildError(f"spend.json unreadable: {exc}") from None
    except ValueError as exc:
        raise BuildError(f"spend.json is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise BuildError("spend.json root must be an object")
    if parse_iso(raw.get("generated_at")) is None:
        raise BuildError("spend.json has no valid generated_at")
    if not isinstance(raw.get("accounts"), list):
        raise BuildError("spend.json has no accounts list")
    return raw


def load_spend(path: Path) -> dict[str, Any]:
    """Load the public projection, retaining only known provider meter IDs."""
    return safe_spend(_load_spend_raw(path))


def reading(pct: float, reset: dt.datetime | None, observed: dt.datetime, minutes: int | None,
            now: dt.datetime) -> dict[str, Any]:
    item: dict[str, Any] = {
        "pct": round(pct, 2), "resets": iso_z(reset) if reset else None,
        "observed": iso_z(observed), "minutes": minutes, "presumed": False,
    }
    if reset is not None and reset <= now:
        item.update(presumed=True, was=round(pct, 2), pct=0.0)
        if minutes:
            length = dt.timedelta(minutes=minutes)
            item["resets"] = iso_z(reset + ((now - reset) // length + 1) * length)
        else:
            item["resets"] = None
    return item


def _sample_alias_names() -> dict[str, str]:
    """Mirror the producer's roster, email, and call_sign resolution for samples."""
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


def read_meters(db: Path, spend: dict[str, Any], now: dt.datetime) -> tuple[dict[str, Any], list[str]]:
    """Latest trustworthy account/meter readings, with aliases kept out of the page."""
    accounts = {item["name"]: item for item in spend.get("accounts", [])}
    names = tuple(accounts)
    if not db.exists() or not names:
        return {}, []
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT provider, account, display, meter, used_percent, resets_at, observed_at, active, window_minutes "
            "FROM (SELECT provider, account, display, meter, used_percent, resets_at, observed_at, active, "
            "window_minutes, row_number() OVER (PARTITION BY provider, account, meter "
            "ORDER BY observed_at DESC) AS rn FROM samples "
            "WHERE provider IN ('Claude', 'Codex', 'Grok', 'Kimi')) WHERE rn <= 4"
        ).fetchall()
    finally:
        connection.close()

    aliases = _sample_alias_names()
    current_displays = {name for name, item in accounts.items() if item.get("provider") == "Claude"}
    best: dict[tuple[str, str], tuple[dt.datetime, dict[str, Any], bool]] = {}
    skipped: dict[tuple[str, str], str] = {}
    for provider, alias, historical_display, meter, pct, reset_raw, observed_raw, active, minutes_raw in rows:
        if provider == "Claude":
            if not isinstance(alias, str) or not alias:
                continue
            display = aliases.get(alias)
            if display is None:
                display = glideslope.call_sign(alias)
                aliases[alias] = display
            if historical_display in current_displays and historical_display != display:
                series = (str(historical_display), meter if isinstance(meter, str) else "")
                skipped.setdefault(series, f"row carries {display}'s store alias")
                continue
        else:
            display = historical_display
        if display not in accounts or accounts[display].get("provider") != provider:
            continue
        if not isinstance(meter, str) or not METER_ID_RE.fullmatch(meter):
            skipped.setdefault((display, ""), "no meter id")
            continue
        series = (display, meter)
        observed, reset = parse_iso(observed_raw), parse_iso(reset_raw)
        if isinstance(pct, bool) or not _number(pct) or pct < 0:
            skipped.setdefault(series, "no usable percent")
            continue
        if observed is None:
            skipped.setdefault(series, "unparseable observed_at")
            continue
        if reset_raw not in (None, "") and reset is None:
            skipped.setdefault(series, "unparseable resets_at")
            continue
        minutes = (int(minutes_raw) if isinstance(minutes_raw, (int, float)) and not isinstance(minutes_raw, bool)
                   and math.isfinite(minutes_raw) and minutes_raw > 0 else None)
        if series not in best or observed > best[series][0]:
            best[series] = (observed, reading(float(pct), reset, observed, minutes, now), bool(active))

    out: dict[str, Any] = {}
    for display in names:
        mine = {meter: value for (name, meter), value in best.items() if name == display}
        if not mine:
            continue
        newest = max(mine, key=lambda meter: mine[meter][0])
        out[display] = {
            "meters": {meter: mine[meter][1] for meter in sorted(mine)},
            "here": mine[newest][2] if accounts[display].get("provider") == "Claude" else False,
            "observed": iso_z(mine[newest][0]),
        }
    notes = [f"{display}/{meter}: {why}" for (display, meter), why in sorted(skipped.items())]
    return out, notes


def script_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def icon_data_uri(path: Path = ICON_SOURCE) -> str:
    try:
        svg = path.read_text()
    except OSError:
        return FALLBACK_ICON
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    svg = re.sub(r">\s+<", "><", svg)
    svg = re.sub(r"\s+", " ", svg).strip().replace('"', "'")
    return "data:image/svg+xml," + svg.replace("%", "%25").replace("#", "%23").replace("<", "%3C").replace(">", "%3E")


def nav_links(output_dir: Path = VIEWS, views_dir: Path = VIEWS) -> dict[str, str | None]:
    """Relative links from the page to existing Detail and History views."""
    output_dir = output_dir.resolve()
    views_dir = views_dir.resolve()
    links: dict[str, str | None] = {}
    for name, path in {
        "detail": views_dir / "deck.html",
        "history": views_dir / "history.html",
    }.items():
        links[name] = quote(os.path.relpath(path.resolve(), output_dir), safe="/.-_~") if path.is_file() else None
    return links


def render(template: str, spend: dict[str, Any], meters: dict[str, Any], build: dict[str, Any], icon: str,
           extension_data: dict[str, Any] | None = None, extension_hook: str = "") -> str:
    for token in TOKENS:
        if template.count(token) != 1:
            raise BuildError(f"template must contain {token} exactly once")
    values = {
        "__SPEND_JSON__": script_json(spend),
        "__METER_JSON__": script_json(meters),
        "__BUILD_JSON__": script_json(build),
        "__GLIDESLOPE_ICON__": icon,
        "__SPEND_EXTENSION_DATA__": script_json(extension_data or {}),
        "__SPEND_EXTENSION_HOOK__": extension_hook,
    }
    return re.sub("|".join(TOKENS), lambda match: values[match.group(0)], template)


def atomic_write(page: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=out.parent, prefix=f".{out.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(page)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, out)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build(spend_path: Path, samples_path: Path, out: Path, template_path: Path,
          now: dt.datetime, *, views_dir: Path = VIEWS,
          extension_data: dict[str, Any] | None = None, extension_hook: str = "") -> str:
    if extension_data is not None and not isinstance(extension_data, dict):
        raise BuildError("extension payload must be a JSON object")
    raw_spend = _load_spend_raw(spend_path)
    spend = safe_spend(raw_spend)
    try:
        template = template_path.read_text()
    except OSError as exc:
        raise BuildError(f"template unreadable: {exc}") from None
    try:
        meters, skipped = read_meters(samples_path, spend, now)
    except sqlite3.Error as exc:
        warn(f"samples.db unreadable ({exc}); building with unknown meter cells")
        meters, skipped = {}, []
    sampled_meter_keys = {
        (display, meter_id)
        for display, account_meters in meters.items()
        for meter_id in account_meters.get("meters", {})
    }
    spend = safe_spend(raw_spend, sampled_meter_keys=sampled_meter_keys)
    for note in skipped:
        warn(f"skipped meter row {note}")
    observations = [parse_iso(item.get("observed")) for item in meters.values()]
    newest = max((value for value in observations if value is not None), default=None)
    timezone = getattr(glideslope.LOCAL_TZ, "key", None)
    metadata = {
        "built_at": iso_z(now),
        "meters_observed_at": iso_z(newest) if newest else None,
        "skipped": skipped,
        "links": nav_links(out.parent, views_dir),
        "timezone": timezone or "UTC",
        "codex_history_note": codex_history_note(spend),
        "curve_accounts": curve_account_names(spend),
    }
    atomic_write(render(template, spend, meters, metadata, icon_data_uri(), extension_data, extension_hook), out)
    count = sum(len(value["meters"]) for value in meters.values())
    return f"spend view built · {len(spend['accounts'])} accounts · {count} meter readings"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spend", type=Path, default=SPEND_JSON)
    parser.add_argument("--samples", type=Path, default=SAMPLES_DB)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--views-dir", type=Path, default=VIEWS,
                        help="directory containing existing Detail and History nav targets")
    parser.add_argument("--extra-payload", type=Path,
                        help="optional private-only JSON object kept outside the public spend whitelist")
    parser.add_argument("--template-hook", type=Path,
                        help="optional trusted HTML/JavaScript inserted after the public page script")
    parser.add_argument("--now", help="UTC or offset ISO instant, for deterministic builds")
    args = parser.parse_args(argv)
    started = time.monotonic()
    now = dt.datetime.now(dt.timezone.utc) if not args.now else parse_iso(args.now)
    if now is None:
        parser.error("--now must be an ISO instant")
    try:
        extension_data: dict[str, Any] | None = None
        if args.extra_payload:
            try:
                extension_data = json.loads(args.extra_payload.read_text())
            except (OSError, ValueError) as exc:
                raise BuildError(f"extension payload unreadable: {exc}") from None
            if not isinstance(extension_data, dict):
                raise BuildError("extension payload must be a JSON object")
        try:
            extension_hook = args.template_hook.read_text() if args.template_hook else ""
        except OSError as exc:
            raise BuildError(f"template hook unreadable: {exc}") from None
        message = build(args.spend, args.samples, args.out, args.template, now,
                        views_dir=args.views_dir, extension_data=extension_data,
                        extension_hook=extension_hook)
    except BuildError as exc:
        sys.stderr.write(f"spend page not built: {exc}\n")
        return 1
    print(f"{message} · {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
