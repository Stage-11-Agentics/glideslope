#!/usr/bin/env python3
"""Publish this satellite's Claude position so the rest of the fleet can read it.

A satellite can read exactly one Claude account live — the one it is logged into
— and only from inside its own GUI session. That second half is not a style
choice: macOS refuses to hand a keychain secret to an SSH session at all
(`errSecInteractionNotAllowed`, exit 36), so a remote machine cannot pull this
gauge no matter how it asks. The read has to happen here, under launchd in the
Aqua session, and be left somewhere plain to collect.

So this writes one small file, `~/.glideslope/satellite.json`:

    {"satellite": "<name>", "observed_at": "…Z", "login_email": "…",
     "snapshot": { "<alias>": { "email": …, "active": true, "limits": [...] } },
     "holds": { "Codex": "…", "Grok": "…" }}

A snapshot row read through a meter token (`"source": "meter-token"`, see
claude_account.py) is a live reading of an account no home here needs to hold: the
satellite that keeps the operator's meter tokens reads every account, logged in or not.
`warnings` carries what claude-account said on stderr (an unknown org, a token named for
one account that bills another), so a problem found here is read where the position is.

`holds` names the single-account providers this satellite is also signed into
(email only: Codex, Grok) — for a provider whose meters are server-side and
account-global, knowing WHO holds it is what lets the fleet read one shared
number as shared instead of presuming a satellite's burn invisible.

`seats`, present only when the Glideslope config sets `[seats] file`, carries the live
rows of that file (a JSON list of coding agents running on cloud sandboxes, written by
whatever launches them) with the file's own `generated_at`, so the reader can judge its
age: `{"generated_at": "…Z", "seats": [{"agent": …, "account": …, …}]}`. A row past its
deadline is dropped here. An absent or unreadable file omits the key and warns.

Numbers, reset clocks, login emails and seat rows only. No credential is read, written, or
stored by this program — `claude-account` borrows the access token Claude Code
already keeps, the Codex login is one decoded claim from a file never held past
that read, and the file below is 0600 and contains no token, refresh token, or key.

Install with tools/install-satellite.sh; it is a plain LaunchAgent underneath.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BEACON = Path.home() / ".glideslope" / "satellite.json"
CLAUDE_JSON = Path.home() / ".claude.json"
PROFILES_DIR = Path.home() / ".claude-profiles"  # extra login homes, one per account
SELECTION = Path.home() / ".claude" / "accounts" / "selection.json"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"  # login identity only, never the tokens
GROK_AUTH = Path.home() / ".grok" / "auth.json"    # login identity only, never the tokens
CANDIDATES = (Path.home() / ".local" / "bin" / "claude-account", Path("/usr/local/bin/claude-account"))
TIMEOUT = 45
CONFIG = Path(os.environ.get("GLIDESLOPE_CONFIG") or (Path.home() / ".glideslope" / "config.toml"))
# What a seat row keeps. The sandbox id stays with the launcher: noise here.
SEAT_FIELDS = ("agent", "model", "effort", "account", "project", "ticket", "role", "run",
               "started", "deadline")
SEATS_MAX_ROWS = 500  # a runaway file must not bloat every beacon read
SEAT_TEXT_MAX = 80


def seats_path(config: Path | None = None) -> Path | None:
    """`[seats] file` from the Glideslope config, or None.

    The beacon is copied to a satellite on its own and may run under a Python
    without tomllib (macOS's /usr/bin/python3 is 3.9), so that case reads the one
    key by hand instead of losing the feature: a plain `file = "<path>"` line under
    `[seats]`, with no `#` in the path and no inline-table form.
    """
    try:
        text = (config or CONFIG).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        import tomllib
    except ModuleNotFoundError:
        value, table = None, None
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("["):
                table = line.strip("[] ")
            elif table == "seats" and line.replace(" ", "").startswith("file="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    else:
        try:
            section = tomllib.loads(text).get("seats")
        except ValueError:
            return None
        value = section.get("file") if isinstance(section, dict) else None
    return Path(os.path.expanduser(value)) if isinstance(value, str) and value else None


def _instant(value: object) -> dt.datetime | None:
    """ISO-8601 or epoch seconds; a zoneless time is UTC (the reader's rule too)."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError, OverflowError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _plain(value: object) -> object:
    """A plain value cut to one short line, or None. The reader re-validates it anyway."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return " ".join(value.split())[:SEAT_TEXT_MAX] if isinstance(value, str) else value


def live_seats(path: Path, now: dt.datetime) -> tuple[dict | None, str | None]:
    """The seat file's live rows as {generated_at, seats}, or None and the reason.

    A row past its deadline is over and is dropped; one with no readable deadline is
    kept (the reader applies the same rule). Only plain values cross: the reader
    re-validates every row, since this file is another program's output.
    """
    try:
        document = json.loads(path.read_text())
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None, f"remote seats: {path} not found"
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"remote seats: {path} is unreadable: {exc}"
    if not isinstance(document, dict) or not isinstance(document.get("seats"), list):
        return None, f"remote seats: {path} has no seats list"
    generated_at = document.get("generated_at")
    if _instant(generated_at) is None:  # no stamp of its own: the file's mtime is the next best
        generated_at = (dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z"))
    rows = []
    for item in document["seats"]:
        if not isinstance(item, dict):
            continue
        deadline = _instant(item.get("deadline"))
        if deadline is not None and deadline <= now:
            continue
        row = {field: _plain(item.get(field)) for field in SEAT_FIELDS}
        rows.append({field: value for field, value in row.items() if value not in (None, "")})
        if len(rows) >= SEATS_MAX_ROWS:
            break
    return {"generated_at": generated_at, "seats": rows}, None


def held_logins() -> dict[str, str]:
    """Single-account providers this satellite is signed into — email only.

    Codex is one decoded claim from the cached id_token; Grok is the email on
    the Grok Build session. Tokens are never kept past the read and never leave
    this machine inside the beacon.
    """
    holds: dict[str, str] = {}
    try:
        tokens = json.loads(CODEX_AUTH.read_text()).get("tokens") or {}
        payload = str(tokens.get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email")
    except (OSError, IndexError, ValueError, AttributeError):
        email = None
    if isinstance(email, str) and email:
        holds["Codex"] = email
    try:
        sessions = json.loads(GROK_AUTH.read_text())
        entry = next((value for value in sessions.values() if isinstance(value, dict)), {})
        grok_email = entry.get("email")
    except (OSError, json.JSONDecodeError, AttributeError):
        grok_email = None
    if isinstance(grok_email, str) and grok_email:
        holds["Grok"] = grok_email
    return holds


def login_email(config_path: Path = CLAUDE_JSON) -> str | None:
    """Who a login home is logged in as — a free local fact, no API call."""
    try:
        config = json.loads(config_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    email = (config.get("oauthAccount") or {}).get("emailAddress")
    return email if isinstance(email, str) and email else None


def login_emails() -> list[str]:
    """Every account this satellite holds a login for: the default home, then each profile home."""
    configs = [CLAUDE_JSON]
    try:
        configs += [entry / ".claude.json" for entry in sorted(PROFILES_DIR.iterdir())
                    if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        pass
    emails: list[str] = []
    for config in configs:
        email = login_email(config)
        if email and email not in emails:
            emails.append(email)
    return emails


def selected_email(logins: list[str]) -> str | None:
    """The account new sessions here start in — claude-account's selection, else the default home."""
    try:
        chosen = json.loads(SELECTION.read_text()).get("email")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        chosen = None
    return chosen if isinstance(chosen, str) and chosen in logins else login_email()


def reader() -> Path | None:
    return next((path for path in CANDIDATES if path.exists() and os.access(path, os.X_OK)), None)


def gauge() -> tuple[dict, str | None, list[str]]:
    """This satellite's live gauge (or an empty snapshot and the reason why), and claude-account's warnings."""
    executable = reader()
    if executable is None:
        return {}, "claude-account is not installed on this satellite", []
    try:
        result = subprocess.run([sys.executable, str(executable), "status", "--json"],
                                capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f"claude-account failed: {exc}", []
    notes = [line.split("claude-account: ", 1)[1] for line in result.stderr.splitlines()
             if line.startswith("claude-account: ")]
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return {}, f"claude-account failed: {detail}", notes
    try:
        snapshot = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {}, f"claude-account returned invalid JSON: {exc}", notes
    if not isinstance(snapshot, dict):
        return {}, "claude-account returned a non-object", notes
    return snapshot, None, notes


def tally(snapshot: dict) -> str:
    """How each published account was read. A stale row is counted, never folded into 'published'."""
    live = sum(1 for row in snapshot.values() if isinstance(row, dict) and not row.get("stale")
               and row.get("source") != "meter-token" and row.get("limits"))
    metered = sum(1 for row in snapshot.values() if isinstance(row, dict) and row.get("source") == "meter-token")
    stale = sum(1 for row in snapshot.values() if isinstance(row, dict) and row.get("stale"))
    return f"{live} by login, {metered} by meter token, {stale} stale"


def publish(payload: dict) -> None:
    """Atomic, 0600. A half-written beacon must never be readable as a position."""
    BEACON.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(BEACON.parent, 0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=BEACON.parent, prefix=f".{BEACON.name}.",
                                         delete=False) as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, BEACON)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    name = os.environ.get("GLIDESLOPE_SATELLITE") or os.uname().nodename.split(".")[0]
    snapshot, error, notes = gauge()
    # The login is published even when the gauge read fails. Knowing WHICH account
    # a satellite holds is what stops the fleet presuming that account unspent —
    # the one lie this instrument must never tell — and it costs no API call, so
    # it must not be lost along with the numbers.
    logins = login_emails()
    payload = {
        "schema": 4,
        "satellite": name,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "login_email": selected_email(logins),
        "login_emails": logins,
        "snapshot": snapshot,
        "holds": held_logins(),
    }
    seat_file = seats_path()
    if seat_file is not None:
        seats, seat_error = live_seats(seat_file, dt.datetime.now(dt.timezone.utc))
        if seats is not None:
            payload["seats"] = seats
        if seat_error:  # never fails the beacon: the seats are left out and the reason travels
            notes = notes + [seat_error]
    if error:
        payload["error"] = error
    if notes:
        payload["warnings"] = notes
    publish(payload)
    stamp = payload["observed_at"]
    print(f"{stamp} {name}: published {len(snapshot)} account(s): {tally(snapshot)}"
          + (f" · {len(payload['seats']['seats'])} remote seat(s)" if "seats" in payload else "")
          + (f" · {error}" if error else ""))
    for note in notes:
        print(f"{stamp} {name}: warning: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
