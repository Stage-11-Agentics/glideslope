#!/usr/bin/env python3
"""Build Glideslope's Detail view and popup preview from one live position.

    python3 views/deck-src/build.py            # cached gauge (no token activity)
    python3 views/deck-src/build.py --fresh    # let glideslope refresh first (locked)

Reads `glideslope.py --json` and <store>/samples.db (default ~/.glideslope), then
injects one self-contained snapshot into:

- deck.tmpl.html → views/deck.html (the rich Detail view)
- popup-src/popup.tmpl.html → views/popup.html (the compact popup preview, sized for a menu-bar panel)

Honesty at the build layer:
- Aliases and emails are mapped to call-signs HERE — the page never receives
  them.
- Trails come only from real samples inside the current window; the page is
  told how much history exists and degrades to dots when it is thin.
- This script never rotates tokens: the default read is the cached gauge.
  refresh.sh owns the gauge step (single writer).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
VIEWS = SRC.parent
ROOT = VIEWS.parent
sys.path.insert(0, str(ROOT))
import glideslope  # noqa: E402  — the config (store path); the position itself is read via the CLI below

DB_PATH = glideslope.STORE_DIR / "samples.db"
# A small sidecar, written beside the sample store this build already reads,
# naming which display currently carries no active subscription and since
# when. history-src/build.py has no provider/credential of its own — it reads
# only the store — so this is the one local, already-computed fact it borrows
# rather than re-deriving. Rewritten in full on every build: it is a mirror of
# the live position, never accumulated.
DORMANT_PATH = DB_PATH.parent / "dormant.json"
MARKER_START = "/*__SNAPSHOT__*/"
MARKER_END = "/*__END_SNAPSHOT__*/"
POPUP_TEMPLATE = VIEWS / "popup-src" / "popup.tmpl.html"
# One drawing, both pages: the mark lives in a real SVG file and is inlined at
# build time, so a self-contained page never carries a stale copy of it.
ICON_SOURCE = VIEWS / "glideslope-icon.svg"
ICON_TOKEN = "__GLIDESLOPE_ICON__"
# Optional input: a spend ledger (spend.json in the store) pricing every request at
# API list rates. Its producer is not part of this repo; the views only read its
# last result, and draw an empty panel without it.
SPEND_PATH = DB_PATH.parent / "spend.json"


def spend_view(path: Path = SPEND_PATH) -> dict | None:
    """The API-equivalent ledger, trimmed to what the pages draw.

    None when absent or malformed: a bad ledger must never stop the minute-ly rebuild.
    """
    try:
        data = json.loads(path.read_text())
        totals = data["totals"]
        if not all(isinstance(totals.get(h), dict) for h in ("d1", "d7", "d30", "all")):
            return None
        daily = data.get("daily") or {}
        if not isinstance(daily.get("days"), list) or not isinstance(daily.get("series"), dict):
            daily = {"days": [], "series": {}}
        keep = ("d1", "d7", "d30", "all", "window", "models", "plan_tier")
        return {
            "generated_at": data.get("generated_at"),
            "collected_at": data.get("collected_at") or {},
            "first_request_at": data.get("first_request_at"),
            "attribution": data.get("attribution") or {},
            "totals": totals,
            "machines": data.get("machines") or {},
            "accounts": [{"name": a.get("name"), "provider": a.get("provider"),
                          **{k: a.get(k) for k in keep}}
                         for a in data.get("accounts") or [] if isinstance(a, dict)],
            "leverage": data.get("leverage") or {},
            "providers": data.get("providers") if isinstance(data.get("providers"), dict) else {},
            "meters": data.get("meters") if isinstance(data.get("meters"), dict) else {},
            "daily": daily,
        }
    except Exception:  # noqa: BLE001 — any shape of broken file degrades to "no ledger"
        return None


def position(fresh: bool) -> dict:
    cmd = [sys.executable, str(ROOT / "glideslope.py"), "--json"]
    if not fresh:
        cmd.append("--no-refresh-claude")
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    if result.returncode != 0:
        raise SystemExit(f"glideslope.py failed: {result.stderr.strip()[:400]}")
    return json.loads(result.stdout)


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def trails_from_store(accounts: list[dict]) -> tuple[dict[str, list[list[float]]], float]:
    """Per display/meter: [[observed_ms, elapsed_pct, used_pct], …] from real
    samples inside the current window. Keeping the observation timestamp lets
    the deck select a genuine history span instead of relabeling one fixed
    trail. Returns (trails, hours_of_history)."""
    if not DB_PATH.exists():
        return {}, 0.0
    con = sqlite3.connect(DB_PATH, timeout=15)
    trails: dict[str, list[list[float]]] = {}
    span_hours = 0.0
    try:
        first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        if first:
            started = parse_ts(first)
            if started:
                span_hours = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 3600
        for account in accounts:
            for limit in account.get("limits", []):
                resets = parse_ts(limit.get("resets_at"))
                minutes = limit.get("window_minutes")
                if not resets or not minutes:
                    continue
                start = resets - dt.timedelta(minutes=minutes)
                rows = con.execute(
                    "SELECT observed_at, used_percent FROM samples"
                    " WHERE provider = ? AND account = ? AND meter = ?"
                    "   AND used_percent IS NOT NULL AND observed_at >= ?"
                    " ORDER BY observed_at",
                    (account["provider"], account["account"],
                     limit.get("meter_id"), start.strftime("%Y-%m-%dT%H:%M:%SZ")),
                ).fetchall()
                points: list[list[float]] = []
                for observed_iso, used in rows:
                    observed = parse_ts(observed_iso)
                    if not observed or observed > resets:
                        continue
                    elapsed = (observed - start) / (resets - start) * 100
                    observed_ms = round(observed.timestamp() * 1000)
                    point = [
                        observed_ms,
                        round(max(0.0, min(100.0, elapsed)), 2),
                        round(used, 2),
                    ]
                    if not points or points[-1] != point:
                        points.append(point)
                if len(points) >= 1:
                    trails[f"{account['display']}/{limit['meter_id']}"] = points
    finally:
        con.close()
    return trails, round(span_hours, 1)


def build_snapshot(pos: dict) -> dict:
    alias_to_display = {a["account"]: a["display"] for a in pos.get("accounts", [])}
    # The configured zone by IANA name; None lets the page use the viewer's own.
    timezone = getattr(glideslope.LOCAL_TZ, "key", None)
    accounts = [{
        "provider": a["provider"],
        "display": a["display"],
        "active": bool(a.get("active")),
        # Login is a place now, not a boolean: the named satellites holding this
        # account, local first. `active` is kept as the local half of the same
        # fact so nothing downstream has to know about the fleet to work.
        "logins": [str(name) for name in (a.get("logins") or [])],
        "plan": a.get("plan") or "",
        "stale": bool(a.get("stale")),
        "observed_at": a.get("observed_at"),
        # A dormant account's subscription is no longer active. It never has
        # logins/active (if it were logged in anywhere, the core would
        # un-dormant it), and its limits carry no usable percent.
        "dormant": bool(a.get("dormant")),
        "dormant_since": a.get("dormant_since"),
        "limits": [{
            "meter_id": l.get("meter_id"),
            "label": l.get("label"),
            "used_percent": l.get("used_percent"),
            "window_minutes": l.get("window_minutes"),
            "resets_at": l.get("resets_at"),
            "anchored": bool(l.get("anchored")),
            # A window advanced past its recorded reset (glideslope.roll_forward_windows).
            # `presumed` = zeroed because this host provably cannot have spent it;
            # otherwise used_percent is None and the new window's burn is unread.
            "presumed": bool(l.get("presumed")),
            "rolled_periods": int(l.get("rolled_periods") or 0),
        } for l in a.get("limits", [])],
    } for a in pos.get("accounts", [])]

    switches = [{
        "at": s.get("at"),
        "from": alias_to_display.get(s.get("from"), "?"),
        "to": alias_to_display.get(s.get("to"), "?"),
        "note": (s.get("note") or "")[:120],
    } for s in (pos.get("switches") or [])[:6]]

    trails, history_hours = trails_from_store(pos.get("accounts", []))

    router = pos.get("openrouter")
    return {
        "timezone": timezone,
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accounts": accounts,
        "openrouter": {
            "weekly_usd": router.get("weekly_usd"),
            "limit_usd": router.get("limit_usd"),
            "remaining_usd": router.get("remaining_usd"),
            "limit_reset": router.get("limit_reset"),
        } if router else None,
        "switches": switches,
        "trails": trails,
        "history_hours": history_hours,
        # The three Anthropic weeklies read as one budget across their offset
        # windows — see glideslope.claude_pool. Both halves of the comparison are
        # means, so it plots on the same beam as every other mark.
        "pool": pos.get("pool"),
        # The same pooled read, computed over weekly_fable instead of
        # weekly_all — same shape, same means-of-means honesty, one meter
        # class substituted for another.
        "pool_fable": pos.get("pool_fable"),
        # Claude + Codex + Grok weeklies (not Kimi) as one price-weighted budget —
        # see glideslope.total_pool.
        "pool_total": pos.get("pool_total"),
        "satellites": pos.get("satellites") or [],
        "local_satellite": next((s["name"] for s in (pos.get("satellites") or [])
                                 if s.get("local")), None),
        "warnings": pos.get("warnings") or [],
        "spend": spend_view(),
    }


def identity_patterns() -> list[str]:
    """What a leaked identity looks like in the serialized snapshot: any email, or a
    configured roster alias standing as a whole JSON string (or as the display half
    of a "Display/meter" key). Matching whole strings, not words, keeps an alias
    that is also an ordinary word ("work") from failing the build on prose."""
    aliases = sorted(alias for alias, name in glideslope.CLAUDE_CALL_SIGNS.items()
                     if alias and alias.lower() != str(name).lower())
    return [rf'"{re.escape(alias)}["/]' for alias in aliases] + [r"[\w.+-]+@[\w-]+\.\w+"]


def snapshot_blob(snapshot: dict) -> str:
    """Serialize the public render model, refusing any private identity."""
    # the page must never see an alias or email — fail the build, not the viewer
    blob = json.dumps(snapshot, separators=(",", ":"))
    # The blob lands inside <script>; provider error strings and labels reach it, so
    # nothing in it may close the tag or open another. JSON allows these escapes.
    blob = (blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    for pattern in identity_patterns():
        found = re.search(pattern, blob, flags=re.IGNORECASE)
        if found:
            raise SystemExit(f"identity leaked into the snapshot: {found.group(0)!r}")
    return blob


def icon_data_uri(path: Path = ICON_SOURCE) -> str:
    """The mark, minified into a data: URI a page can wear as its favicon."""
    svg = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
    svg = re.sub(r">\s+<", "><", svg)
    svg = re.sub(r"\s+", " ", svg).strip().replace('"', "'")
    return "data:image/svg+xml," + (svg.replace("%", "%25").replace("#", "%23")
                                       .replace("<", "%3C").replace(">", "%3E"))


def render_page(template_path: Path, blob: str, icon: str) -> str:
    template = template_path.read_text()
    start = template.index(MARKER_START) + len(MARKER_START)
    end = template.index(MARKER_END)
    page = template[:start] + blob + template[end:]
    if ICON_TOKEN not in page:
        raise SystemExit(f"{template_path.name} has nowhere to wear the mark ({ICON_TOKEN})")
    return page.replace(ICON_TOKEN, icon)


def atomic_write(page: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    output_mode = out.stat().st_mode & 0o777 if out.exists() else 0o644
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=out.parent, prefix=f".{out.name}.", delete=False
        ) as handle:
            handle.write(page)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, output_mode)
        os.replace(temporary, out)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_dormant_sidecar(accounts: list[dict], path: Path = DORMANT_PATH) -> None:
    """`{display: dormant_since}` for every dormant Claude account — the one
    fact history-src/build.py borrows to stop extending a dead account's trail
    past the day it went dormant. Written even when empty, so a dormancy that
    ends is reflected here too rather than leaving a stale entry behind."""
    dormant = {a["display"]: a.get("dormant_since")
               for a in accounts if a.get("dormant")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dormant, sort_keys=True))
    except OSError:
        pass  # the sidecar is an optimization for history's clip; never fail the build over it


def write_deck(pos: dict, out: Path, popup_out: Path | None = None) -> dict:
    """Build the rich view and, when requested, its compact popup preview."""
    snapshot = build_snapshot(pos)
    write_dormant_sidecar(pos.get("accounts", []))
    blob = snapshot_blob(snapshot)
    icon = icon_data_uri()
    detail_page = render_page(SRC / "deck.tmpl.html", blob, icon)
    popup_page = render_page(POPUP_TEMPLATE, blob, icon) if popup_out is not None else None

    atomic_write(detail_page, out)
    if popup_page is not None and popup_out is not None:
        atomic_write(popup_page, popup_out)

    trails = snapshot["trails"]
    destinations = f"Detail view → {out}"
    if popup_out is not None:
        destinations += f"; popup preview → {popup_out}"
    print(f"views built: {len(snapshot['accounts'])} accounts, "
          f"{sum(len(a['limits']) for a in snapshot['accounts'])} meters, "
          f"{len(trails)} trails over {snapshot['history_hours']}h of samples; "
          f"{destinations}")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fresh = "--fresh" in args
    from_stdin = "--position-stdin" in args
    unknown = [arg for arg in args if arg not in {"--fresh", "--position-stdin"}]
    if unknown:
        raise SystemExit(f"unknown argument: {unknown[0]}")
    if fresh and from_stdin:
        raise SystemExit("--fresh and --position-stdin are mutually exclusive")

    if from_stdin:
        try:
            pos = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"position stdin is not valid JSON: {exc}") from exc
        if not isinstance(pos, dict):
            raise SystemExit("position stdin root must be an object")
    else:
        pos = position(fresh)

    write_deck(pos, VIEWS / "deck.html", VIEWS / "popup.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
