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

`holds` names the single-account providers this satellite is also signed into
(email only: Codex, Grok) — for a provider whose meters are server-side and
account-global, knowing WHO holds it is what lets the fleet read one shared
number as shared instead of presuming a satellite's burn invisible.

Numbers, reset clocks, and login emails only. No credential is read, written, or
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


def gauge() -> tuple[dict, str | None]:
    """This satellite's live gauge, or an empty snapshot and the reason why."""
    executable = reader()
    if executable is None:
        return {}, "claude-account is not installed on this satellite"
    try:
        result = subprocess.run([sys.executable, str(executable), "status", "--json"],
                                capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, f"claude-account failed: {exc}"
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return {}, f"claude-account failed: {detail}"
    try:
        snapshot = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {}, f"claude-account returned invalid JSON: {exc}"
    return (snapshot, None) if isinstance(snapshot, dict) else ({}, "claude-account returned a non-object")


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
    snapshot, error = gauge()
    # The login is published even when the gauge read fails. Knowing WHICH account
    # a satellite holds is what stops the fleet presuming that account unspent —
    # the one lie this instrument must never tell — and it costs no API call, so
    # it must not be lost along with the numbers.
    logins = login_emails()
    payload = {
        "schema": 3,
        "satellite": name,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "login_email": selected_email(logins),
        "login_emails": logins,
        "snapshot": snapshot,
        "holds": held_logins(),
    }
    if error:
        payload["error"] = error
    publish(payload)
    print(f"{name}: published {len(snapshot)} account(s)" + (f" · {error}" if error else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
