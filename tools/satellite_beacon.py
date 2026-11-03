#!/usr/bin/env python3
"""Publish this satellite's Claude position so the rest of the fleet can read it.

A satellite can read exactly one Claude account live — the one it is logged into
— and only from inside its own GUI session. That second half is not a style
choice: macOS refuses to hand a keychain secret to an SSH session at all
(`errSecInteractionNotAllowed`, exit 36), so a remote machine cannot pull this
gauge no matter how it asks. The read has to happen here, under launchd in the
Aqua session, and be left somewhere plain to collect.

So this writes one small file, `~/.glideslope/satellite.json`:

    {"satellite": "<name>", "observed_at": "…Z", "login_email": "…", "login_id": "email#org",
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

`seats`, present only when the Glideslope config sets `[seats] file`, carries the rows
of that file (a JSON list of coding agents running on cloud sandboxes, written by
whatever launches them) with the file's own `generated_at`, so the reader can judge its
age: `{"generated_at": "…Z", "seats": [{"agent": …, "account": …, …}], "live_total": n}`,
plus `"truncated": true` when either list hit the row cap. Rows that ended in the last day ride along so
the reader can settle duplicates before it drops them (see seat_document). An absent or
unreadable file omits the key and warns; it never stops the beacon.

`env_tokens`, present when a running Claude session was launched with
CLAUDE_CODE_OAUTH_TOKEN, counts those sessions per token fingerprint. Such a session bills the
token's account, not this machine's login, and its transcript records no organization, so the
beacon also keeps `~/.glideslope/session-tokens.json`, the spend ledger's evidence of which
session ran under which token (see record_token_sessions). A token is read from a process's
environment only to be hashed. It is never sent anywhere, stored, logged or compared in the
clear. The account behind a fingerprint comes from claude-account's own map, which
`claude-account whose <file>` fills from a token file the operator names; a fingerprint nothing
has named is reported as a warning and credits nothing.

Numbers, reset clocks, login emails, seat rows and token fingerprints only. No credential is
written or stored by this program — `claude-account` borrows the access token Claude Code
already keeps, the Codex login is one decoded claim from a file never held past
that read, and the file below is 0600 and contains no token, refresh token, or key.

Install with tools/install-satellite.sh; it is a plain LaunchAgent underneath.
"""

from __future__ import annotations

import base64
import datetime as dt
import glob
import hashlib
import json
import os
import re
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
SESSION_DIRS = (Path.home() / ".claude" / "sessions",)  # one <pid>.json per live Claude session
METER_ORGS = Path.home() / ".claude" / "accounts" / ".meter-orgs.json"  # fingerprint -> org
TOKEN_EVIDENCE = Path.home() / ".glideslope" / "session-tokens.json"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_READ_TIMEOUT = 10
TIMEOUT = 45
CONFIG = Path(os.environ.get("GLIDESLOPE_CONFIG") or (Path.home() / ".glideslope" / "config.toml"))
# What a seat row keeps. The sandbox id stays with the launcher: noise here.
SEAT_FIELDS = ("agent", "model", "effort", "account", "project", "ticket", "role", "run",
               "started", "deadline")
SEATS_MAX_ROWS = 500  # live rows carried, and again ended ones: a runaway file must not bloat a beacon
SEATS_ENDED_SECONDS = 24 * 3600  # ended rows carried this long, so an older copy cannot revive one
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


def seat_document(path: Path, now: dt.datetime) -> tuple[dict | None, str | None]:
    """The seat file as the beacon carries it, or None and the reason.

    Live rows come first. Rows that ended within SEATS_ENDED_SECONDS follow them:
    an ended row is how the reader learns that an older copy of the same seat
    (from another source, with a later deadline) is over, so it is never dropped
    here before the reader has settled which copy is newest. A row with no
    readable deadline counts as live (nothing proves it ended). Past the row cap
    on either list (a cut ending matters as much as a cut live row: it can leave an
    older live copy standing) the document says `truncated`, and the reader marks
    the whole result incomplete. Only plain values cross: the reader re-validates
    every row, since this file is another program's output.

    `generated_at` is the file's own when it gives one, the file's mtime only when
    it gives none, and carried as-is when it is present but unreadable, so the
    reader treats it as stale rather than as freshly written.
    """
    try:
        document = json.loads(path.read_text())
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None, f"remote seats: {path} not found"
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"remote seats: {path} is unreadable: {exc}"
    if not isinstance(document, dict) or not isinstance(document.get("seats"), list):
        return None, f"remote seats: {path} has no seats list"
    generated_at = document.get("generated_at")
    if generated_at in (None, ""):  # no stamp of its own: the file's mtime is the next best
        generated_at = (dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z"))
    elif _instant(generated_at) is None:
        plain = _plain(generated_at)
        generated_at = plain if plain not in (None, "") else "unreadable"
    live, ended = [], []
    for item in document["seats"]:
        if not isinstance(item, dict):
            continue
        row = {field: _plain(item.get(field)) for field in SEAT_FIELDS}
        row = {field: value for field, value in row.items() if value not in (None, "")}
        deadline = _instant(item.get("deadline"))
        if deadline is None or deadline > now:
            live.append(row)
        elif (now - deadline).total_seconds() <= SEATS_ENDED_SECONDS:
            ended.append(row)
    carried = {"generated_at": generated_at,
               "seats": live[:SEATS_MAX_ROWS] + ended[:SEATS_MAX_ROWS],
               "live_total": len(live), "ended_total": len(ended)}
    if len(live) > SEATS_MAX_ROWS or len(ended) > SEATS_MAX_ROWS:
        carried["truncated"] = True
    return carried, None


def session_files() -> list[Path]:
    """Every live-session file: the default home, then each profile home."""
    paths: list[Path] = []
    for directory in SESSION_DIRS:
        paths += sorted(directory.glob("*.json")) if directory.is_dir() else []
    paths += [Path(path) for path in sorted(glob.glob(str(PROFILES_DIR / "*" / "sessions" / "*.json")))]
    return paths


def process_token_fingerprint(pid: int) -> str | None:
    """The fingerprint of the token in one process's environment, or None. The value is dropped here.

    Matches claude_account.token_fingerprint. `ps -E` prints argv and then the environment, so
    the environment is what follows the plain argv: a prompt that quotes the variable is argv,
    and an empty value is no token.
    """
    def read(*flags: str) -> str:
        return subprocess.run(["ps", *flags, "-o", "command=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=TOKEN_READ_TIMEOUT, check=False).stdout.rstrip("\n")
    try:
        command, text = read("-ww"), read("-Eww")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    environment = text[len(command):] if command and text.startswith(command) else None
    if environment is None:
        return None  # argv and environment cannot be told apart: no sighting beats a wrong one
    marker = TOKEN_VARIABLE + "="
    index = environment.rfind(marker)
    if index < 0 or (index and not environment[index - 1].isspace()):
        return None
    value = re.match(r"\S*", environment[index + len(marker):]).group(0)
    return hashlib.sha256(value.encode()).hexdigest()[:10] if value else None


def _millis_iso(value: object) -> str | None:
    """A session file's epoch-milliseconds start as ISO-8601 UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    moment = dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def token_sightings() -> list[dict]:
    """Each live session running under an env token: {session, fp, start}."""
    sightings = []
    for path in session_files():
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        session, pid = record.get("sessionId"), record.get("pid")
        if not isinstance(session, str) or isinstance(pid, bool) or not isinstance(pid, int):
            continue
        fingerprint = process_token_fingerprint(pid)
        if fingerprint:
            sightings.append({"session": session, "fp": fingerprint,
                              "start": _millis_iso(record.get("startedAt"))})
    return sightings


def record_token_sessions(sightings: list[dict], now: str, path: Path | None = None,
                          orgs_path: Path | None = None) -> tuple[dict[str, int], list[str]]:
    """Fold sightings into the evidence file. Returns (sessions per fingerprint, unnamed fingerprints).

    {"schema": 1, "tokens": {fp: org}, "sessions": {id: [{fp, start, first_seen, last_seen}]}}.
    A session gets a new span when its token or its process start changes. Spans are only
    ever extended, so the file is the whole history of what this machine saw. A file that
    cannot be read raises rather than being replaced: history is not rebuilt from one tick.
    """
    path = path or TOKEN_EVIDENCE
    try:
        document = json.loads(path.read_text())
        sessions, tokens = document["sessions"], dict(document.get("tokens") or {})
    except FileNotFoundError:
        sessions, tokens = {}, {}
    try:
        known = json.loads((orgs_path or METER_ORGS).read_text())
        known = known if isinstance(known, dict) else {}
    except (OSError, ValueError):
        known = {}
    counts: dict[str, int] = {}
    for sighting in sightings:
        spans = sessions.setdefault(sighting["session"], [])
        counts[sighting["fp"]] = counts.get(sighting["fp"], 0) + 1
        if spans and spans[-1].get("fp") == sighting["fp"] and spans[-1].get("start") == sighting["start"]:
            spans[-1]["last_seen"] = now
        else:
            spans.append({"fp": sighting["fp"], "start": sighting["start"] or now,
                          "first_seen": now, "last_seen": now})
    # A name, once learned, is kept: a run whose map has lost a fingerprint must not un-credit
    # sessions it already named.
    seen = {span.get("fp") for spans in sessions.values() for span in spans}
    tokens.update({fp: known[fp] for fp in seen if isinstance(known.get(fp), str)})
    write_private({"schema": 1, "tokens": tokens, "sessions": sessions}, path)
    return counts, sorted(fp for fp in counts if fp not in tokens)


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


def login_id(config_path: Path = CLAUDE_JSON) -> str | None:
    """Who a login home is logged in as — a free local fact, no API call.

    "email#organization": one email can hold several accounts, one per organization
    (glideslope.py, login_id). An email alone when the login names no organization.
    """
    try:
        config = json.loads(config_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    account = account if isinstance(account, dict) else {}
    email, org = account.get("emailAddress"), account.get("organizationUuid")
    if not isinstance(email, str) or not email.strip():
        return None
    org = org.strip().lower() if isinstance(org, str) else ""
    return f"{email.strip().lower()}#{org}" if org else email.strip().lower()


def login_ids() -> list[str]:
    """Every account this satellite holds a login for: the default home, then each profile home."""
    configs = [CLAUDE_JSON]
    try:
        configs += [entry / ".claude.json" for entry in sorted(PROFILES_DIR.iterdir())
                    if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        pass
    logins: list[str] = []
    for config in configs:
        login = login_id(config)
        if login and login not in logins:
            logins.append(login)
    return logins


def selected_login(logins: list[str]) -> str | None:
    """The account new sessions here start in — claude-account's selection, else the default home."""
    try:
        stored = json.loads(SELECTION.read_text())
        email = str(stored.get("email") or "").strip().lower()
        org = str(stored.get("org_uuid") or "").strip().lower()
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        email, org = "", ""
    for login in logins:
        held_email, _, held_org = login.partition("#")
        if email and held_email == email and (not org or held_org == org):
            return login
    return login_id()


def email_of(login: str | None) -> str | None:
    return login.partition("#")[0] if login else None


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
    write_private(payload, BEACON)


def write_private(payload: dict, target: Path) -> None:
    """Atomic, 0600. A half-written beacon must never be readable as a position."""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=f".{target.name}.",
                                         delete=False) as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
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
    logins = login_ids()
    selected = selected_login(logins)
    payload = {
        "schema": 4,
        "satellite": name,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # Emails for readers that predate organizations; ids say which account under each.
        "login_email": email_of(selected),
        "login_emails": list(dict.fromkeys(filter(None, map(email_of, logins)))),
        "login_id": selected,
        "login_ids": logins,
        "snapshot": snapshot,
        "holds": held_logins(),
    }
    # Seats are optional: whatever goes wrong reading them, the beacon still publishes,
    # without them, and the reason travels with it.
    try:
        seat_file = seats_path()
        seats, seat_error = (seat_document(seat_file, dt.datetime.now(dt.timezone.utc))
                             if seat_file is not None else (None, None))
    except Exception as exc:  # noqa: BLE001
        seats, seat_error = None, f"remote seats could not be read: {type(exc).__name__}: {exc}"
    if seats is not None:
        payload["seats"] = seats
    if seat_error:
        notes = notes + [seat_error]
    # Token evidence is optional in the same way: it can never stop the beacon, and an exception's
    # text stays out of the note, so nothing read from a process can reach a log.
    try:
        token_counts, unnamed = record_token_sessions(token_sightings(), payload["observed_at"])
    except Exception as exc:  # noqa: BLE001
        token_counts, unnamed = {}, []
        notes = notes + [f"session token evidence not recorded: {type(exc).__name__}"]
    if token_counts:
        payload["env_tokens"] = token_counts
    notes = notes + [f"{token_counts[fp]} running session(s) use token {fp}, which no token file has "
                     "named; run claude-account whose <file>" for fp in unnamed]
    if error:
        payload["error"] = error
    if notes:
        payload["warnings"] = notes
    publish(payload)
    stamp = payload["observed_at"]
    print(f"{stamp} {name}: published {len(snapshot)} account(s): {tally(snapshot)}"
          + (f" · {payload['seats']['live_total']} remote seat(s)"
             + (", truncated at the row cap" if payload["seats"].get("truncated") else "")
             if "seats" in payload else "")
          + (f" · {error}" if error else ""))
    for note in notes:
        print(f"{stamp} {name}: warning: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
