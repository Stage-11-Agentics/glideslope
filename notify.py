#!/usr/bin/env python3
"""Glideslope's threshold alerts: draw the approach, deliver it, once per window.

The sampler already reads the position every five minutes. This is what turns the
one reading that deserves interrupting you into a native banner — a window on the
account you are ON crossing NOTIFY_PERCENT.

Three things it owns, none of which belong in the sampler's loop:

**Once per window, ever.** The sampler runs every five minutes; a naive threshold
test would fire twelve banners an hour for as long as you stayed over the line.
Every alert is keyed by the window *instance* — account, meter, and that window's
own reset instant — so crossing 90% notifies exactly once and the next window,
which carries a new reset, is free to notify again on its own merits.

**The picture.** A notification that says "92%" makes you open something to learn
what it means. The attachment is the approach plot for that one window: the beam,
your mark against it, and the red flight path to the ceiling — the same geometry,
the same palette, and the same three colours the deck uses, so the banner reads as
this instrument rather than as a generic alert.

**Delivery.** Through the configured sink: a macOS notification by default, or
one JSON POST to a local listener (`[notify] sink = "url"`) when a menu-bar host
wants to deliver the banner under its own identity and icon. If the sink is down
the alert is dropped, not queued — a usage warning that arrives hours late is
worse than one that never came; it stays unsent and fires on the next tick.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import glideslope
from glideslope import (
    LOCAL_TZ, NOTIFY_PERCENT, NOTIFY_SINK, NOTIFY_URL, STORE_DIR, best_alternative, format_countdown,
    format_percent, iso_utc, notifiable_windows, parse_timestamp,
)

STATE_PATH = STORE_DIR / "notified.json"
IMAGE_DIR = STORE_DIR / "alerts"
SAMPLES_DB = STORE_DIR / "samples.db"
DELIVER_TIMEOUT = 6

# The deck's own tokens. The banner is a piece of the same instrument, so it may
# not invent a palette: clay/kraft/brick for the three accounts, ink for the beam,
# and the one red this project spends on capacity for the path to the ceiling.
VOID, PANEL, BEZEL, GRID = (5, 7, 11), (13, 18, 25), (29, 40, 54), (24, 34, 48)
INK, MUTED, BEAM = (232, 238, 246), (125, 138, 156), (108, 122, 142)
CAPPED, CAPPED_DIM, BANK = (255, 81, 103), (96, 34, 44), (67, 183, 207)
ACCOUNT_HUES = {"Alpha": (217, 119, 87), "Bravo": (212, 162, 127), "Charlie": (176, 80, 60),
                "Delta": (194, 139, 44)}
MONO = ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf")


# ------------------------------------------------------------------ dedup state

def clock_key(when: dt.datetime) -> str:
    """A reset instant as a key: to the nearest minute. The provider reports the
    same deadline as 02:59:59, 03:00:00 or 03:00:01 from one read to the next, and
    keying on the raw second made one window three instances, each free to warn."""
    return iso_utc((when + dt.timedelta(seconds=30)).replace(second=0, microsecond=0))


def _normalized(key: str) -> str:
    head, _, clock = key.rpartition("|")
    when = parse_timestamp(clock)
    return f"{head}|{clock_key(when)}" if head and when is not None else key


def window_key(alert: dict[str, Any]) -> str:
    """One alert per window INSTANCE — the reset instant is what makes it one.

    Keying on account+meter alone would silence the next window too; keying on
    the clock as well means a fresh window is a fresh chance to warn. An early
    reset keeps the clock but starts a new budget, so it names the instance too:
    without it, the 90% already sent before the reset silenced the second climb.
    The reset instant rides on the meter so the clock stays the key's last field,
    which is what `prune` reads.
    """
    meter = alert["meter_id"]
    rebased = parse_timestamp(alert.get("rebased_at"))
    if rebased is not None:
        meter = f"{meter}@{clock_key(rebased)}"
    return f"{alert['account']}|{meter}|{clock_key(alert['resets_at'])}"


# ------------------------------------------------------------------ early resets
# A used reset is news the moment it lands and stale an hour later: the banner
# is the flash, and a sampler that was down through it should not fire it late.
RESET_BANNER_WINDOW = dt.timedelta(hours=2)


def reset_alerts(accounts: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    """One alert per account whose budget was reset early within the banner window.

    Any account, not just the one logged in here: a reset is something the
    operator did on purpose, and seeing it land is the confirmation. The weekly
    all-models meter speaks for the account when it has the event; siblings that
    only inherited it never speak first.
    """
    alerts: list[dict[str, Any]] = []
    for account in accounts:
        if account.get("dormant"):
            continue
        candidates = []
        for limit in account.get("limits", []):
            resets = limit.get("early_resets") or []
            reset = parse_timestamp(limit.get("resets_at"))
            duration = limit.get("window_minutes") or 0
            if not resets or reset is None or reset <= now or duration < 24 * 60:
                continue
            last = resets[-1]
            at = parse_timestamp(last.get("at"))
            if at is None or now - at > RESET_BANNER_WINDOW:
                continue
            rank = (bool(last.get("inherited")), limit.get("meter_id") != "weekly_all")
            candidates.append((rank, limit, last, at, reset))
        if not candidates:
            continue
        _, limit, last, at, reset = min(candidates, key=lambda c: c[0])
        percent = limit.get("used_percent")
        alerts.append({
            "kind": "reset",
            "account": account.get("display"),
            "alias": account.get("account"),
            "provider": account.get("provider"),
            "meter_id": limit.get("meter_id"),
            "label": limit.get("label"),
            "used_percent": float(percent) if percent is not None else float(last.get("to_percent") or 0),
            "resets_at": reset,
            "window_minutes": limit.get("window_minutes"),
            "rebased_at": at,
            "early_resets": limit.get("early_resets") or [],
            "from_percent": float(last.get("from_percent") or 0),
            "from_plan": last.get("from_plan"),
            "to_plan": last.get("to_plan"),
            "exhausts_at": None,
            "floor": False,
        })
    return alerts


def reset_key(alert: dict[str, Any]) -> str:
    """Once per reset. The window's clock stays last so `prune` forgets it at the deadline."""
    return (f"{alert['account']}|{alert['meter_id']}!reset@{clock_key(alert['rebased_at'])}"
            f"|{clock_key(alert['resets_at'])}")


def load_state(path: Path = STATE_PATH) -> dict[str, str]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    # keys written before clock_key existed carry the raw second; read them rounded
    return ({_normalized(str(k)): str(v) for k, v in value.items()}
            if isinstance(value, dict) else {})


def prune(state: dict[str, str], now: dt.datetime) -> dict[str, str]:
    """Forget windows that have already reset — the key can never recur."""
    kept = {}
    for key, sent in state.items():
        reset = parse_timestamp(key.rsplit("|", 1)[-1])
        if reset is None or reset > now:
            kept[key] = sent
    return kept


def save_state(state: dict[str, str], path: Path = STATE_PATH) -> None:
    """Atomic, 0600. A half-written ledger would re-fire every alert in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as handle:
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------- wording

def _clock(when: dt.datetime, now: dt.datetime) -> str:
    """A wall-clock time, wearing its day only when a bare clock would mislead.

    "gone by 1:33 AM" is unambiguous a few hours out and a riddle six days out,
    which is exactly the range a 7-day window covers.
    """
    local = when.astimezone(LOCAL_TZ)
    same_day = local.date() == now.astimezone(LOCAL_TZ).date()
    return local.strftime("%-I:%M %p" if same_day else "%a %-I:%M %p")


def compose(alert: dict[str, Any], alternative: dict[str, Any] | None,
            now: dt.datetime) -> tuple[str, str]:
    """Title and body. Says what happened, when it bites, and what to do about it.

    The third clause is the point. "Charlie is at 92%" is a fact you already had
    a dashboard for; "and Bravo has 27 points of room" is the reason to read a
    banner instead of opening one.
    """
    window = "5 HOUR" if alert["meter_id"] == "session" else (
        "7 DAY · Fable" if alert["meter_id"] == "weekly_fable" else "7 DAY")
    used = format_percent(alert["used_percent"]) + ("+" if alert["floor"] else "")
    title = f"{alert['account']} · {window} at {used}"

    parts = [f"Resets in {format_countdown(alert['resets_at'] - now)}"
             f" ({_clock(alert['resets_at'], now)})."]
    if alert["exhausts_at"] is not None:
        parts.append(f"At this rate it is gone by {_clock(alert['exhausts_at'], now)}.")
    if alternative is not None:
        # best_alternative ranks each account by its BINDING weekly — usually
        # all-models, sometimes Fable — and names which one in `meter_id`. Only
        # worth saying when it's the less obvious of the two.
        on_fable = " on Fable" if alternative.get("meter_id") == "weekly_fable" else ""
        parts.append(f"{alternative['display']} has the most room{on_fable} —"
                     f" {format_percent(alternative['used_percent'])} against a"
                     f" ◆ {format_percent(alternative['pace_percent'])}.")
    return title, " ".join(parts)


def compose_reset(alert: dict[str, Any], now: dt.datetime) -> tuple[str, str]:
    """Title and body for a used reset: what it cleared, how long the fresh budget
    runs, and how much faster than a week's even burn it can now be spent."""
    window = "7 DAY · Fable" if alert["meter_id"] == "weekly_fable" else "7 DAY"
    changed = alert.get("from_plan") and alert.get("to_plan")
    title = f"{alert['account']} · plan changed" if changed else f"{alert['account']} · reset used"
    span = (alert["resets_at"] - alert["rebased_at"]).total_seconds()
    parts = []
    if changed:
        parts.append(f"Plan {alert['from_plan']} → {alert['to_plan']}.")
    parts.append(f"{window} was {format_percent(alert['from_percent'])},"
                 f" now {format_percent(alert['used_percent'])}.")
    parts.append(f"A fresh budget until {_clock(alert['resets_at'], now)}"
                 f" ({format_countdown(alert['resets_at'] - now)} left).")
    if span > 0 and alert.get("window_minutes"):
        multiple = alert["window_minutes"] * 60 / span
        if multiple >= 1.05:
            parts.append(f"Even burn now runs {multiple:.1f}× the weekly pace.")
    return title, " ".join(parts)


# ----------------------------------------------------------------- the approach

def _font(size: int):
    from PIL import ImageFont
    for path in MONO:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _dashed(draw, start: tuple[float, float], end: tuple[float, float],
            colour: tuple[int, int, int], width: int, on: int = 9, off: int = 9) -> None:
    """A dashed segment. Pillow has no dash support, so walk it."""
    (x0, y0), (x1, y1) = start, end
    span = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if span <= 0:
        return
    step = (on + off) / span
    position = 0.0
    while position < 1:
        tail = min(1.0, position + on / span)
        draw.line([(x0 + (x1 - x0) * position, y0 + (y1 - y0) * position),
                   (x0 + (x1 - x0) * tail, y0 + (y1 - y0) * tail)], fill=colour, width=width)
        position += step


def approach_track(alert: dict[str, Any], db: Path = SAMPLES_DB) -> list[tuple[float, float]]:
    """The real samples inside this window: [(elapsed %, used %), …].

    The same store, keyed the same way, as the deck's trails — so the banner shows
    the track that actually happened rather than a schematic of it. Missing or
    unreadable store is not an error: the picture simply loses its history and
    keeps its position.
    """
    reset, duration = alert.get("resets_at"), alert.get("window_minutes")
    if not db.exists() or not isinstance(reset, dt.datetime) or not duration:
        return []
    start = reset - dt.timedelta(minutes=duration)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT observed_at, used_percent FROM samples"
            " WHERE provider = ? AND account = ? AND meter = ?"
            "   AND used_percent IS NOT NULL AND observed_at >= ?"
            " ORDER BY observed_at",
            (alert.get("provider") or "Claude", alert.get("alias"), alert.get("meter_id"),
             start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    span = (reset - start).total_seconds()
    points: list[tuple[float, float]] = []
    for observed_iso, used in rows:
        observed = parse_timestamp(observed_iso)
        if observed is None or observed > reset:
            continue
        elapsed = max(0.0, min(100.0, (observed - start).total_seconds() / span * 100))
        point = (round(elapsed, 2), round(float(used), 2))
        if not points or points[-1] != point:
            points.append(point)
    return points


def draw_approach(alert: dict[str, Any], now: dt.datetime, size: int = 512) -> Path | None:
    """The one window's approach, in the instrument's own geometry and palette.

    Four things, in the order they matter at thumbnail size: the beam you are
    being judged against, the track you actually flew, the mark you are at, and
    the red path to where that rate lands you. The numbers live in the title,
    where they can be read; the picture's whole job is the shape — *above the
    line, heading for the ceiling* — legible at 64 pixels.

    The percent is set in the corner opposite the mark, because the corner a mark
    is in is the one corner a label can never share with it.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    reset, duration = alert["resets_at"], alert.get("window_minutes")
    if not duration:
        return None
    start = reset - dt.timedelta(minutes=duration)
    span = (reset - start).total_seconds()
    elapsed = max(0.0, min(100.0, (now - start).total_seconds() / span * 100))
    used = max(0.0, min(100.0, alert["used_percent"]))

    image = Image.new("RGB", (size, size), VOID)
    draw = ImageDraw.Draw(image)
    pad = int(size * 0.105)
    frame = (pad, pad, size - pad, size - pad)
    inner = frame[2] - frame[0]
    px = lambda e: frame[0] + e / 100 * inner            # noqa: E731 — plot coords
    py = lambda u: frame[3] - u / 100 * inner            # noqa: E731

    draw.rectangle(frame, fill=PANEL, outline=BEZEL, width=2)
    for value in (25, 50, 75):
        draw.line([(px(value), frame[1]), (px(value), frame[3])], fill=GRID, width=1)
        draw.line([(frame[0], py(value)), (frame[2], py(value))], fill=GRID, width=1)
    # the ceiling: the thing the red path terminates at, drawn as a place
    draw.line([(frame[0], py(100)), (frame[2], py(100))], fill=CAPPED_DIM, width=3)
    # the beam — reference, not subject, so it stays dim and thin under the track.
    # After an early reset the budget is judged against its own beam, from the
    # reset to the deadline; the window's original beam stays as a ghost.
    rebased = parse_timestamp(alert.get("rebased_at"))
    origin = (0.0, 0.0)
    if rebased is not None and start < rebased < reset:
        resets = alert.get("early_resets") or []
        floor = float((resets[-1] if resets else {}).get("to_percent") or 0)
        origin = ((rebased - start).total_seconds() / span * 100, floor)
        _dashed(draw, (px(0), py(0)), (px(100), py(100)), GRID, 2, on=4, off=9)
        _dashed(draw, (px(origin[0]), py(origin[1])), (px(100), py(100)), BEAM, 3, on=4, off=9)
    else:
        _dashed(draw, (px(0), py(0)), (px(100), py(100)), BEAM, 3, on=4, off=9)

    hue = ACCOUNT_HUES.get(str(alert["account"]), INK)
    track = [point for point in approach_track(alert) if point[1] is not None]
    if len(track) >= 2:
        draw.line([(px(e), py(u)) for e, u in track] + [(px(elapsed), py(used))],
                  fill=hue, width=max(3, int(size * 0.009)), joint="curve")

    # the checkered drop: every early reset, hung from where the meter fell from
    for event in alert.get("early_resets") or []:
        at = parse_timestamp(event.get("at"))
        if at is None or not start < at < reset:
            continue
        _checkered_drop(draw, px((at - start).total_seconds() / span * 100),
                        py(float(event.get("from_percent") or 0)),
                        py(float(event.get("to_percent") or 0)), size)

    # the flight path — forward from the mark, at the rate burned since the
    # budget began (the window's start, or its latest reset)
    run_e, run_u = elapsed - origin[0], used - origin[1]
    if run_u > 0 and run_e > 0.1:
        slope = run_u / run_e
        hit = elapsed + (100 - used) / slope
        exhausts = hit <= 100
        end = ((px(min(hit, 100)), py(100)) if exhausts
               else (px(100), py(min(used + slope * (100 - elapsed), 100))))
        _dashed(draw, (px(elapsed), py(used)), end, CAPPED if exhausts else BANK,
                max(4, int(size * 0.012)), on=11, off=9)
        if exhausts:
            radius = size * 0.026
            draw.ellipse([end[0] - radius, end[1] - radius, end[0] + radius, end[1] + radius],
                         fill=CAPPED, outline=VOID, width=3)

    # the mark: a solid disc for a 7 DAY budget, a hollow triangle for a 5 HOUR
    # throttle, inside the ink ring the plot puts on the logged-in account
    mx, my, r = px(elapsed), py(used), size * 0.032
    if alert["meter_id"] == "session":
        draw.polygon([(mx, my - r), (mx - r, my + r * 0.82), (mx + r, my + r * 0.82)],
                     outline=hue, width=max(3, int(size * 0.008)))
    else:
        draw.ellipse([mx - r, my - r, mx + r, my + r], fill=hue)
    ring = r * 1.9
    draw.ellipse([mx - ring, my - ring, mx + ring, my + ring], outline=INK, width=3)

    # The number goes in whichever corner the drawing actually left empty. A
    # fixed corner — even one chosen from the used percent — sooner or later
    # lands on the track, and a figure printed over the line it describes is
    # worse than no figure at all.
    inset = int(size * 0.028)
    ink = [(mx, my)] + [(px(e), py(u)) for e, u in track[:: max(1, len(track) // 24)]]
    corners = [(frame[0] + inset, frame[1] + inset, "la"), (frame[2] - inset, frame[1] + inset, "ra"),
               (frame[0] + inset, frame[3] - inset, "ld"), (frame[2] - inset, frame[3] - inset, "rd")]
    cx, cy, anchor = max(corners, key=lambda c: min((c[0] - ax) ** 2 + (c[1] - ay) ** 2
                                                    for ax, ay in ink))
    draw.text((cx, cy), f"{used:.0f}%", font=_font(int(size * 0.105)),
              fill=CAPPED if used >= 99.5 else INK, anchor=anchor)

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(IMAGE_DIR, 0o700)
    # One file per account/meter: overwritten on redraw, never accumulating.
    kind = "-reset" if alert.get("kind") == "reset" else ""
    path = IMAGE_DIR / f"{alert['account']}-{alert['meter_id']}{kind}.png".lower()
    image.save(path, "PNG")
    return path


def _checkered_drop(draw, x: float, top: float, bottom: float, size: int) -> None:
    """A thin chequered cloth from `top` down to `bottom`, with a flag at its head:
    the same mark the views hang where a reset cleared the meter."""
    cell = max(3, round(size * 0.012))
    left = x - cell
    rows = max(1, int((bottom - top) / cell))
    for row in range(rows):
        for column in range(2):
            colour = INK if (row + column) % 2 == 0 else VOID
            y0 = top + row * cell
            draw.rectangle([left + column * cell, y0, left + (column + 1) * cell, y0 + cell],
                           fill=colour)
    for row in range(3):
        for column in range(4):
            colour = INK if (row + column) % 2 == 0 else VOID
            x0, y0 = x + cell + column * cell, top + row * cell
            draw.rectangle([x0, y0, x0 + cell, y0 + cell], fill=colour)
    draw.rectangle([left, top, left + 2 * cell, top + rows * cell], outline=MUTED, width=1)


# --------------------------------------------------------------------- delivery

def deliver(title: str, body: str, image: Path | None = None, *, url: str = NOTIFY_URL,
            sink: str = NOTIFY_SINK) -> bool:
    """Deliver one banner through the configured sink. Never raises; a dropped
    alert is not an outage.

    "macos" posts a notification through osascript (no image: Notification Center
    takes none from a script). "url" POSTs one JSON object — `title`, `body`,
    `tenant`, and `image` as a path — to the configured listener; a host that
    ignores keys it does not know still delivers the words. "none" delivers
    nothing and reports so, which leaves the alert unsent for the next tick.
    """
    if sink == "none" or (sink == "url" and not url):
        return False
    if sink != "url":
        script = 'display notification {} with title {}'.format(
            json.dumps(body, ensure_ascii=False), json.dumps(title, ensure_ascii=False))
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True,
                                    timeout=DELIVER_TIMEOUT)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    payload = {"title": title, "body": body, "tenant": "glideslope"}
    if image is not None:
        payload["image"] = str(image)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=DELIVER_TIMEOUT) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run(accounts: list[dict[str, Any]], now: dt.datetime, *,
        threshold: float = NOTIFY_PERCENT, state_path: Path = STATE_PATH,
        url: str = NOTIFY_URL) -> list[str]:
    """Select, dedup, draw, deliver. Returns the keys actually notified.

    The state is written only for alerts that were DELIVERED. An alert dropped
    because the sink was down stays unsent, so it fires on the next tick once the
    sink is back rather than being silently marked as told.
    """
    alerts = notifiable_windows(accounts, now, threshold=threshold)
    state = prune(load_state(state_path), now)
    sent: list[str] = []
    for alert in alerts:
        key = window_key(alert)
        if key in state:
            continue
        alternative = best_alternative(accounts, now, exclude=alert["account"],
                                       threshold=threshold)
        title, body = compose(alert, alternative, now)
        if deliver(title, body, draw_approach(alert, now), url=url):
            state[key] = iso_utc(now)
            sent.append(key)
    for alert in reset_alerts(accounts, now):
        key = reset_key(alert)
        if key in state:
            continue
        title, body = compose_reset(alert, now)
        if deliver(title, body, draw_approach(alert, now), url=url):
            state[key] = iso_utc(now)
            sent.append(key)
    save_state(state, state_path)
    return sent


def main() -> int:
    """Read the position and deliver anything over the line (also the manual path)."""
    import argparse

    parser = argparse.ArgumentParser(description="Deliver Glideslope threshold alerts.")
    parser.add_argument("--threshold", type=float, default=NOTIFY_PERCENT)
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be sent; deliver nothing, record nothing")
    args = parser.parse_args()

    namespace = glideslope.argparse.Namespace(
        json=True, no_refresh_claude=False, claude_snapshot=glideslope.DEFAULT_CLAUDE_SNAPSHOT,
        claude_roster=glideslope.CLAUDE_ROSTER,
        codex_snapshot=None, kimi_snapshot=None, grok_snapshot=None, skip_claude=False,
        skip_codex=True, skip_kimi=True, skip_grok=True, skip_openrouter=True,
        skip_harness_spend=True, skip_satellites=False, skip_seats=True,
        no_switches=True, max_age_seconds=glideslope.CLAUDE_CACHE_SECONDS)
    now, accounts, *_ = glideslope.gather(namespace)

    if args.dry_run:
        for alert in notifiable_windows(accounts, now, threshold=args.threshold):
            alternative = best_alternative(accounts, now, exclude=alert["account"],
                                           threshold=args.threshold)
            title, body = compose(alert, alternative, now)
            already = window_key(alert) in load_state()
            print(f"{'[already sent] ' if already else ''}{title}\n  {body}")
        for alert in reset_alerts(accounts, now):
            title, body = compose_reset(alert, now)
            already = reset_key(alert) in load_state()
            print(f"{'[already sent] ' if already else ''}{title}\n  {body}")
        return 0
    for key in run(accounts, now, threshold=args.threshold):
        print(f"notified {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
