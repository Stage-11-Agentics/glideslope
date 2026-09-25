#!/usr/bin/env python3
"""Build Glideslope's History view — the approach plot flown on a real clock.

    python3 views/history-src/build.py

Reads ONLY <store>/samples.db. This page never touches a
provider, a credential, or a lock: it is the store, drawn. That is deliberate —
history is answerable from what we already wrote down, so opening it can never
cost a token or disturb the single-writer discipline the live read observes.

The translation from the deck's normalized approach plot to a real timeline:

- The deck plots ONE window per meter, with x = "% of the window elapsed". Every
  account's week is stretched onto the same 0-100 rail, which is what makes the
  ◆ even-burn beam a single diagonal shared by everybody.
- Here x is wall-clock. A week no longer stretches to fit, so the shared diagonal
  becomes a LADDER of diagonals: one beam per window instance, running from that
  window's own start at 0% to its own reset at 100%. The read is identical —
  above your beam is ahead of budget, below is money left on the table — but now
  every account's ladder sits at its true phase, and weeks stack up behind you.

Reconstructing the sawtooth (the whole problem, and its answer):

The store holds point-in-time gauges, not window records. Windows are recovered
from the samples themselves:

- `resets_at` jitters by a second or two between reads, so instances are grouped
  with a tolerance, not by equality.
- `resets_at` is NULL after a reset until first use ("unanchored"). Those zeros
  are real observations, not gaps, and they belong to the window whose beam has
  already started — so they are folded forward into it.
- Codex re-anchors on first use, Claude tumbles on a fixed weekly clock. Both are
  handled by the same rule: a window's beam runs `resets_at - window_minutes` to
  `resets_at`, and it is CLIPPED to the stretch during which it was the live
  window. A reset that lands early (Codex, 2026-08-08) then reads as the truth it
  is — the track dropped before its beam ran out — instead of a drawing error.
- A missed sample costs resolution, not information, but a five-day lockout is not
  a plateau. Sampling gaps are emitted explicitly so the page can break the line
  there and say so, rather than drawing a confident straight lie across it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
VIEWS = SRC.parent
sys.path.insert(0, str(VIEWS.parent))
import glideslope  # noqa: E402  — the config (store path) only; this build reads no provider

DB_PATH = glideslope.STORE_DIR / "samples.db"
# Written by deck-src/build.py beside the store it already reads: which
# display currently carries no active subscription, and since when. This page
# touches no provider and no credential of its own — it borrows this one
# already-computed local fact rather than re-deriving it, so a dormant
# account's trail can stop at the day it went dormant without this build
# calling out to glideslope.py itself.
DORMANT_PATH = DB_PATH.parent / "dormant.json"
MARKER_START = "/*__SNAPSHOT__*/"
MARKER_END = "/*__END_SNAPSHOT__*/"
ICON_SOURCE = VIEWS / "glideslope-icon.svg"
ICON_TOKEN = "__GLIDESLOPE_ICON__"

# Two reads within this of each other describe the same window; the gauge's own
# reset clock wanders by a second or two between calls.
RESET_TOLERANCE_S = 300
# A roll the clock did not report: the percent falls from something substantial
# to near nothing. It has to be BOTH, because Codex's weekly is a rolling window
# whose percent genuinely sags mid-cycle as old usage ages out — a bare "fell by
# 5 points" rule reads every one of those sags as a reset and shatters the week.
DROP_FROM = 15.0
DROP_TO = 5.0
# Past this the line is broken and marked unread. Deliberately well above a
# missed sample or two: a 35-minute hiccup is resolution lost, and drawing a
# break there would cry wolf often enough that the real ones — a locked-out
# account, a laptop shut for two days — stop reading as anything.
GAP_S = 90 * 60

# Meters the history view carries. `spend` (OpenRouter, dollars) has no percent
# and no window to fly, so it is not a series here.
METERS = {
    ("Claude", "weekly_all"):     ("7 DAY", "all models", "week", 0),
    ("Claude", "weekly_fable"):   ("7 DAY", "Fable", "week", 1),
    ("Claude", "session"):        ("5 HOUR", "", "session", 2),
    ("Codex", "codex"):           ("7 DAY", "all models", "week", 0),
    ("Codex", "codex_bengalfox"): ("7 DAY", "Spark", "week", 1),
    ("Kimi", "weekly_all"):       ("7 DAY", "all models", "week", 0),
    ("Kimi", "session"):          ("5 HOUR", "", "session", 2),
    ("Grok", "weekly_all"):       ("7 DAY", "all models", "week", 0),
    ("Grok", "session"):          ("5 HOUR", "", "session", 2),
}
FAMILY_RANK = {"Claude": 0, "Codex": 1, "Kimi": 2, "Grok": 3}
ACCOUNT_RANK = {"Alpha": 0, "Bravo": 1, "Charlie": 2, "Delta": 3, "Codex": 4, "Kimi": 5, "Grok": 6}
COLOR_KEY = {"Alpha": "alpha", "Bravo": "bravo", "Charlie": "charlie", "Delta": "delta",
             "Codex": "codex", "Kimi": "kimi", "Grok": "grok"}


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ms(value: dt.datetime) -> int:
    return round(value.timestamp() * 1000)


def read_rows(con: sqlite3.Connection) -> dict[tuple, list[dict]]:
    """Every percent observation, grouped by (provider, display, meter)."""
    series: dict[tuple, list[dict]] = {}
    rows = con.execute(
        "SELECT provider, display, meter, used_percent, window_minutes,"
        "       resets_at, observed_at, active"
        "  FROM samples WHERE meter != 'spend' AND used_percent IS NOT NULL"
        " ORDER BY provider, display, meter, observed_at"
    ).fetchall()
    for provider, display, meter, used, minutes, resets, observed, active in rows:
        if (provider, meter) not in METERS:
            continue
        at = parse_ts(observed)
        if at is None:
            continue
        series.setdefault((provider, display, meter), []).append({
            "t": ms(at),
            "u": float(used),
            "reset": ms(parse_ts(resets)) if resets else None,
            "minutes": int(minutes) if minutes else None,
            "active": bool(active),
        })
    # One observation per timestamp: the unique key is (…, observed_at), but the
    # same read can be written twice under two jittered reset clocks.
    for key, points in series.items():
        deduped: list[dict] = []
        for point in points:
            if deduped and deduped[-1]["t"] == point["t"]:
                # keep the one that carries a reset clock — it is the more complete read
                if deduped[-1]["reset"] is None and point["reset"] is not None:
                    deduped[-1] = point
                continue
            deduped.append(point)
        series[key] = deduped
    return series


def segment(points: list[dict]) -> list[dict]:
    """Split one meter's samples into window instances.

    A new instance begins when the reset clock moves beyond tolerance, or when
    the percent falls hard inside an unchanged clock (a roll the gauge reported
    late). Unanchored samples — reset clock NULL, nothing spent yet — start a
    provisional instance that is folded into the next real window if that
    window's beam had already begun.
    """
    instances: list[dict] = []
    current: dict | None = None
    for point in points:
        reset, minutes = point["reset"], point["minutes"]
        start_new = current is None
        if current is not None:
            same_clock = (
                (reset is None and current["reset"] is None)
                or (reset is not None and current["reset"] is not None
                    and abs(reset - current["reset"]) <= RESET_TOLERANCE_S * 1000)
            )
            previous = current["points"][-1]["u"]
            if not same_clock:
                start_new = True
            elif previous >= DROP_FROM and point["u"] <= DROP_TO:
                start_new = True
        if start_new:
            current = {"reset": reset, "minutes": minutes, "points": []}
            instances.append(current)
        else:
            # let a slowly-drifting clock update its own instance
            if reset is not None:
                current["reset"] = reset
            if minutes:
                current["minutes"] = minutes
        current["points"].append(point)

    # Fold an unanchored run forward: those zeros belong to the window whose beam
    # was already running when they were taken.
    folded: list[dict] = []
    for index, instance in enumerate(instances):
        if instance["reset"] is None and index + 1 < len(instances):
            nxt = instances[index + 1]
            if nxt["reset"] and nxt["minutes"]:
                beam_start = nxt["reset"] - nxt["minutes"] * 60_000
                if instance["points"][0]["t"] >= beam_start:
                    nxt["points"] = instance["points"] + nxt["points"]
                    continue
        folded.append(instance)
    return folded


def compress(points: list[dict], keep: set[int]) -> list[list[float]]:
    """Plateau reduction. The gauge moves in 1% steps, so the honest shape is a
    staircase: keeping only the corners of each tread reproduces it exactly while
    dropping ~90% of the samples. Timestamps adjacent to a gap are pinned by the
    caller so a break can never be mistaken for a flat stretch."""
    kept: list[list[float]] = []
    for index, point in enumerate(points):
        edge = index == 0 or index == len(points) - 1 or index in keep
        turn = (index > 0 and points[index - 1]["u"] != point["u"]) or (
            index + 1 < len(points) and points[index + 1]["u"] != point["u"])
        if edge or turn:
            kept.append([point["t"], point["u"]])
    return kept


def build_series(key: tuple, points: list[dict], now_ms: int) -> dict:
    provider, display, meter = key
    label, scope, klass, rank = METERS[(provider, meter)]
    instances = segment(points)

    gaps: list[list[int]] = []
    pinned: set[int] = set()
    for index in range(1, len(points)):
        if points[index]["t"] - points[index - 1]["t"] > GAP_S * 1000:
            gaps.append([points[index - 1]["t"], points[index]["t"]])
            pinned.add(index - 1)
            pinned.add(index)

    out_instances = []
    for index, instance in enumerate(instances):
        first = instance["points"][0]["t"]
        last = instance["points"][-1]["t"]
        minutes = instance["minutes"]
        reset = instance["reset"]
        beam_start = reset - minutes * 60_000 if (reset and minutes) else None
        # The stretch during which this instance was the live window. It opens at
        # its own beam start (a window is running before it is spent) and closes
        # where the next one opened — or at its reset, whichever comes FIRST.
        # Both clips matter: Codex has rolled early, and an unread account has
        # sat past its reset with no sample to prove the roll.
        span_from = min(first, beam_start) if beam_start else first
        if index > 0:
            span_from = max(span_from, instances[index - 1]["points"][-1]["t"])
        open_window = index == len(instances) - 1
        span_to = instances[index + 1]["points"][0]["t"] if not open_window else (
            reset if reset else last)
        if reset and not open_window:
            span_to = min(span_to, reset)
        span_to = max(span_to, last)
        out_instances.append({
            "beam": [beam_start, reset] if beam_start else None,
            "span": [span_from, span_to],
            "peak": max(p["u"] for p in instance["points"]),
            "open": open_window,
        })

    return {
        "key": f"{display}/{meter}",
        "provider": provider,
        "display": display,
        "meter": meter,
        "label": label,
        "scope": scope,
        "class": klass,
        "color": COLOR_KEY.get(display, "router"),
        "minutes": points[-1]["minutes"],
        "rank": [FAMILY_RANK.get(provider, 9), ACCOUNT_RANK.get(display, 9), rank],
        "last": [points[-1]["t"], points[-1]["u"]],
        "peak": max(p["u"] for p in points),
        # A meter that has never moved off zero across the whole store: real, and
        # worth being able to see, but it should not cost a lane by default.
        "idle": max(p["u"] for p in points) == 0,
        "gaps": gaps,
        "points": compress(points, pinned),
        "instances": out_instances,
    }


def load_dormant(path: Path = DORMANT_PATH) -> dict[str, int]:
    """`{display: dormant_since_ms}` — absent or unreadable reads as "nobody
    dormant," never as an error: this sidecar is an optional optimization, not
    a dependency history can be broken by."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, int] = {}
    for display, since in (raw or {}).items():
        at = parse_ts(since)
        if at is None:
            continue
        # A bare "YYYY-MM-DD" (what glideslope.py actually writes) parses to a
        # NAIVE datetime, whose .timestamp() would silently read it in this
        # host's local zone rather than UTC — the same day-boundary bug in
        # every other shape this store is careful never to have.
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        out[display] = ms(at)
    return out


def clip_dormant(grouped: dict[tuple, list[dict]], dormant: dict[str, int]) -> dict[tuple, list[dict]]:
    """A dormant account's trail stops at the day it went dormant — earlier
    history is untouched, later samples (there should be none, but a stale
    sidecar or a race is not a reason to draw a lie) are dropped."""
    if not dormant:
        return grouped
    clipped: dict[tuple, list[dict]] = {}
    for key, points in grouped.items():
        provider, display, meter = key
        cutoff = dormant.get(display) if provider == "Claude" else None
        kept = [p for p in points if p["t"] < cutoff] if cutoff is not None else points
        if kept:
            clipped[key] = kept
    return clipped


def build_snapshot(db_path: Path = DB_PATH, dormant_path: Path = DORMANT_PATH) -> dict:
    if not db_path.exists():
        raise SystemExit(f"no sample store at {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        grouped = read_rows(con)
    finally:
        con.close()
    grouped = clip_dormant(grouped, load_dormant(dormant_path))
    if not grouped:
        raise SystemExit("sample store holds no percent observations")

    now = dt.datetime.now(dt.timezone.utc)
    now_ms = ms(now)
    series = [build_series(key, points, now_ms)
              for key, points in grouped.items() if len(points) >= 2]
    series.sort(key=lambda s: s["rank"])
    if not series:
        # Every meter either never had 2 points, or dormancy clipped it below
        # that floor — clipping to nothing is a real outcome (an account
        # dormant since before this store's history begins), not a bug.
        raise SystemExit("sample store holds no percent observations")

    first = min(s["points"][0][0] for s in series)
    last = max(s["points"][-1][0] for s in series)
    return {
        "timezone": getattr(glideslope.LOCAL_TZ, "key", None),
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "now": now_ms,
        "extent": [first, max(last, now_ms)],
        "sample_count": sum(len(p) for p in grouped.values()),
        "point_count": sum(len(s["points"]) for s in series),
        "series": series,
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
    """Serialize the render model, refusing any private identity. Same guard the
    deck build carries: the page sees call-signs, never an alias or an email."""
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
    svg = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
    svg = re.sub(r">\s+<", "><", svg)
    svg = re.sub(r"\s+", " ", svg).strip().replace('"', "'")
    return "data:image/svg+xml," + (svg.replace("%", "%25").replace("#", "%23")
                                       .replace("<", "%3C").replace(">", "%3E"))


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=VIEWS / "history.html")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--json", action="store_true",
                        help="print the snapshot instead of building the page")
    args = parser.parse_args(argv)

    snapshot = build_snapshot(args.db, dormant_path=args.db.parent / "dormant.json")
    blob = snapshot_blob(snapshot)
    if args.json:
        print(blob)
        return 0

    template = (SRC / "history.tmpl.html").read_text()
    start = template.index(MARKER_START) + len(MARKER_START)
    end = template.index(MARKER_END)
    page = template[:start] + blob + template[end:]
    if ICON_TOKEN not in page:
        raise SystemExit(f"history.tmpl.html has nowhere to wear the mark ({ICON_TOKEN})")
    page = page.replace(ICON_TOKEN, icon_data_uri())
    atomic_write(page, args.out)

    days = (snapshot["extent"][1] - snapshot["extent"][0]) / 86_400_000
    print(f"history built: {len(snapshot['series'])} series, "
          f"{sum(len(s['instances']) for s in snapshot['series'])} windows, "
          f"{snapshot['point_count']} of {snapshot['sample_count']} samples kept, "
          f"{days:.1f} days → {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
