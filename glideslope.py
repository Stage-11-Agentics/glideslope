#!/usr/bin/env python3
"""Report the live Claude, Codex, Kimi and Grok subscription position in one stable format.

Claude is read through `claude-account`, which borrows the access token Claude Code
already keeps and only while it is still valid; Codex through the documented
app-server protocol, never its credential files; Kimi with a static platform key
over one documented GET. Grok's consumer meter lives behind the Grok Build login
(`~/.grok/auth.json`): the sampler reads the current access token and, when it is
expired, refreshes it the same way Grok does — atomically, in place, never copying
the token anywhere else. That refresh is the one credential write in this program,
and it exists because a six-hour access token cannot otherwise stay live on the
sampler's 60s clock.

Only the logged-in Claude account is readable, by construction. The other accounts are
served from the state journaled while THEY were live, with their windows advanced to
the live ones — see roll_forward_windows(). The journal contains usage state only and
costs no credential at all.

Claude accounts are reported by call-sign (Alpha, Bravo, Charlie… — declared in the
config, or assigned in roster order), never by their store aliases or emails; Codex,
Kimi and Grok are single-account subscriptions and carry their provider's name. Every
provider lands in the same account shape and is rendered by the same tables; see
PROVIDERS.md for each one's read path.

Configuration is one optional file, ~/.glideslope/config.toml (see config.example.toml);
GLIDESLOPE_CONFIG overrides its path and GLIDESLOPE_HOME overrides the store directory.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import json
import os
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent


# ----------------------------------------------------------------- configuration
# One optional TOML file. Every key has a default that makes a fresh install work
# with a single Claude login and nothing declared. Unreadable config is reported
# once on stderr and ignored: a typo in the config must never take the position
# down with it.
CONFIG_PATH = Path(os.environ.get("GLIDESLOPE_CONFIG") or (Path.home() / ".glideslope" / "config.toml"))


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Read the config file, or {} when it is absent or unreadable."""
    if not path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11: run on defaults rather than not at all
        print(f"glideslope: Python 3.11+ is needed to read {path}; running with defaults",
              file=sys.stderr)
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        print(f"glideslope: ignoring config {path}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


CONFIG = load_config()


def _config_table(name: str) -> dict[str, Any]:
    table = CONFIG.get(name)
    return table if isinstance(table, dict) else {}


def _config_path(value: Any, default: Path) -> Path:
    return Path(os.path.expanduser(str(value))) if value else default


def _config_timezone(name: Any) -> dt.tzinfo:
    """The zone every human-facing time is rendered in: the config's, else the system's."""
    if name:
        try:
            return ZoneInfo(str(name))
        except Exception:  # noqa: BLE001 — an unknown zone name falls back, loudly
            print(f"glideslope: unknown timezone {name!r} in config; using the system zone",
                  file=sys.stderr)
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


# The store: sample DB, provider caches, alert state, the optional spend ledger.
STORE_DIR = _config_path(os.environ.get("GLIDESLOPE_HOME") or CONFIG.get("store"),
                         Path.home() / ".glideslope")
DEFAULT_CLAUDE_SNAPSHOT = STORE_DIR / "account-snapshot.json"
# Last-known-good raw reads for the providers whose transport can fail
# transiently (Codex's app-server, Kimi's HTTP). A provider that times out must
# degrade to its last observation, never vanish from the position.
PROVIDER_CACHE_DIR = STORE_DIR / "provider-cache"
PROVIDER_CACHE_MAX_SECONDS = 7 * 24 * 3600
# Hard ceiling on one provider's live read, spawn included (see call_with_deadline).
PROVIDER_DEADLINE_SECONDS = 30.0
CLAUDE_JSON = Path.home() / ".claude.json"  # who's logged in — a free local fact, no API
# Extra login homes (claude-account, 2026-09-12): one CLAUDE_CONFIG_DIR per account,
# each with its own .claude.json, so a machine can hold every account at once.
CLAUDE_PROFILES_DIR = Path.home() / ".claude-profiles"
# Which of those logins new sessions start in. A pointer to an email, never a token.
CLAUDE_SELECTION = Path.home() / ".claude" / "accounts" / "selection.json"
CLAUDE_HISTORY = Path.home() / ".claude" / "history.jsonl"  # includes local slash commands
# Satellites — the machines that can hold a Claude login. A satellite reads
# exactly ONE account live: the one it is logged into. So the set of live-readable
# accounts is the set of satellites, not a property of this machine. The remote
# ones publish a beacon (tools/satellite_beacon.py under launchd); we only ever
# read it. Names are the configured satellite names, never hostnames-as-identity.
SATELLITE_BEACON_PATH = Path.home() / ".glideslope" / "satellite.json"
SATELLITE_BEACON_REMOTE = "~/.glideslope/satellite.json"  # same path, the satellite's own home
# Declared in the config as [[satellites]] name/host pairs; none by default.
REMOTE_SATELLITES = tuple(
    {"name": str(item.get("name") or item.get("host")), "host": str(item.get("host"))}
    for item in (CONFIG.get("satellites") or []) if isinstance(item, dict) and item.get("host"))
SATELLITE_SSH_TIMEOUT = 8
# An ssh host from the config is a name (or user@name), never an option string.
SATELLITE_HOST_PATTERN = __import__("re").compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$")
SATELLITE_CACHE_SECONDS = 120  # politeness to the tailnet; the beacon itself is 5-minutely
SATELLITE_BEACON_MAX_AGE_SECONDS = 3600  # older than this, the gauge is a floor, not a read
SWITCH_LOG = Path.home() / ".claude" / "accounts" / ".switch-log.jsonl"
# claude-account's own roster: identity only, plus (since 2026-09-11) a dormancy
# flag it now carries when a subscription lapses. No credential ever lives here.
CLAUDE_ROSTER = Path.home() / ".claude" / "accounts" / "roster.json"
LOGIN_OBSERVER_STATE = STORE_DIR / "claude-login-state.json"
LOGIN_OBSERVER_LOCK = STORE_DIR / ".claude-login-observer.lock"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
# The sanctioned local key file (0600, KEY=value lines). Read only for static API
# keys, and only as a fallback — it is what makes the launchd sampler, which
# carries no shell environment, able to read Kimi and OpenRouter.
KEYS_FILE = _config_path(CONFIG.get("keys_file"), STORE_DIR / "keys.txt")
HARNESS_SPEND_DIR = STORE_DIR / "telemetry"
LOCAL_TZ = _config_timezone(CONFIG.get("timezone"))
CLAUDE_CACHE_SECONDS = 50  # under the sampler's 60s cadence, so every sample is a real read
SWITCH_HISTORY_LIMIT = 8

# The line past which a window on the account you are ON is worth interrupting
# you for, and the windows that can cross it. Fable is included deliberately: it
# is a 7-day budget like the other, and it is the one that most often binds while
# all-models still has room — the usual reason to swap at all.
NOTIFY_PERCENT = float(_config_table("notify").get("percent") or 90.0)
NOTIFY_METERS = ("session", "weekly_all", "weekly_fable")
# Where a banner goes: "macos" (an osascript notification, the default), "url"
# (POST one JSON object to NOTIFY_URL — how a menu-bar host with its own
# listener takes delivery), or "none".
NOTIFY_SINK = str(_config_table("notify").get("sink") or "macos").lower()
NOTIFY_URL = str(_config_table("notify").get("url") or "")

# The bucket labels claude-account journals, shortest-first, for the switch row.
SWITCH_BUCKETS = [("session (5h)", "5h"), ("weekly (all models)", "wk"), ("weekly (Fable)", "Fable")]

CLAUDE_ROW_ORDER = {"weekly_all": 0, "weekly_fable": 1, "session": 2}
CLAUDE_BUCKETS = {
    "session (5h)": ("session", "5h session", 300),
    "weekly (all models)": ("weekly_all", "Weekly · all models", 10_080),
    "weekly (Fable)": ("weekly_fable", "Weekly · Fable", 10_080),
}

# This machine's name. Satellites are addressed by name in every surface; the
# hostname is only how we happen to learn it when the config does not say.
LOCAL_SATELLITE = (str(_config_table("satellite").get("name") or "")
                   or socket.gethostname().split(".")[0] or "local")

# Call-signs. The store alias is an implementation detail; these are the names.
# Declared under [claude] call_signs = { key = "Name" } where a key is a roster
# alias or, when it contains "@", an email (emails are the same on every machine;
# aliases are per-machine bookkeeping). Any account the config does not name takes
# the next free NATO letter in the order it is first seen, which is roster order —
# stable across runs for as long as the roster is.
CLAUDE_CALL_SIGNS: dict[str, str] = {
    str(alias): str(name) for alias, name in (_config_table("claude").get("call_signs") or {}).items()}
NATO_ALPHABET = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel",
                 "India", "Juliett", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa")
CLAUDE_PLAN = str(_config_table("claude").get("plan") or "Max 20x")
# The plan is not in the usage gauge, but the tier IS in claude-account's roster,
# which rewrites it on every status read. One account once came back from a
# renewal on Pro, and a board that still called it Max 20x was the reason nobody
# noticed Fable had stopped being included. Unknown tiers keep their raw string:
# a name we do not recognize is still a fact, and better than a confident wrong one.
CLAUDE_PLAN_BY_TIER = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_ai": "Pro",
}
CODEX_PLAN = str(_config_table("codex").get("plan") or "20x")
CODEX_ANCHOR_FIELD = "_glideslopeAnchored"
CODEX_ZERO_PROBE_SECONDS = 1.1
CODEX_AUTH_JSON = Path.home() / ".codex" / "auth.json"  # login identity only, never the tokens

# Kimi Code coding plan. The API only reports a coarse membership level
# (LEVEL_INTERMEDIATE), never the tier's marketing name, so the multiplier is an
# operator-declared fact here exactly as the Claude and Codex plans are; the raw
# level travels alongside it as `plan_raw`.
KIMI_PLAN = str(_config_table("kimi").get("plan") or "Coding 15x")
KIMI_PLAN_QUOTA_MINUTES = 10_080  # the plan quota cycles every 7 days from the subscription date
KIMI_SESSION_MINUTES = 300  # the rolling burst window, reported as 300 TIME_UNIT_MINUTE
KIMI_TIME_UNIT_MINUTES = {
    "TIME_UNIT_SECOND": 1 / 60,
    "TIME_UNIT_MINUTE": 1,
    "TIME_UNIT_HOUR": 60,
    "TIME_UNIT_DAY": 1_440,
}

# SuperGrok / Grok Build. The consumer weekly pool is behind the Grok CLI login,
# not an API key. Access tokens last ~6 hours; Grok refreshes them in the
# background while a Grok process is running, and this program does the same
# operation when the sampler ticks against an expired token.
GROK_PLAN = str(_config_table("grok").get("plan") or "SuperGrok")
GROK_AUTH_JSON = Path.home() / ".grok" / "auth.json"
GROK_VERSION_JSON = Path.home() / ".grok" / "version.json"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_OIDC_ISSUER = "https://auth.x.ai"
GROK_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
GROK_TOKEN_SKEW_SECONDS = 60
GROK_WEEKLY_MINUTES = 10_080
# A Stop/SessionStart hook writes this numbers-only snapshot. The sampler prefers
# it when it is younger than this, so a turn that just finished is visible before
# the next live GET; after it ages out we hit billing ourselves (covers grok.com /
# phone burn while Build is closed).
GROK_HOOK_SNAPSHOT = STORE_DIR / "grok-billing.json"
GROK_HOOK_MAX_AGE_SECONDS = 90


class PositionError(RuntimeError):
    """A provider could not supply a usable position snapshot."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value, dt.timezone.utc)
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def cache_provider_read(name: str, raw: dict[str, Any], observed_at: dt.datetime) -> None:
    """Store a provider's raw read as last-known-good. Never raises.

    Only the transport is unreliable, not the numbers — so the raw response is
    kept verbatim together with the moment it was observed. Re-normalizing it
    later reproduces the same absolute reset clocks the live read produced.
    """
    try:
        PROVIDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(PROVIDER_CACHE_DIR, 0o700)
        path = PROVIDER_CACHE_DIR / f"{name}.json"
        with tempfile.NamedTemporaryFile(
            "w", dir=PROVIDER_CACHE_DIR, prefix=f".{name}.", delete=False
        ) as handle:
            json.dump({"observed_at": iso_utc(observed_at), "snapshot": raw}, handle,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        pass  # a cache write must never break a position read


def cached_provider_read(
    name: str, now: dt.datetime, *, max_age_seconds: int = PROVIDER_CACHE_MAX_SECONDS
) -> tuple[dict[str, Any], dt.datetime] | None:
    """The last good raw read for a provider, with its true observation time.

    Returns None when nothing is cached, the file is unreadable, or the read is
    older than the longest window it could describe — beyond that it can only
    report windows that have certainly rolled.
    """
    path = PROVIDER_CACHE_DIR / f"{name}.json"
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        return None
    observed_at = parse_timestamp(payload.get("observed_at"))
    if observed_at is None or (now - observed_at).total_seconds() > max_age_seconds:
        return None
    return payload["snapshot"], observed_at


def read_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise PositionError(f"snapshot not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PositionError(f"snapshot is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PositionError(f"snapshot root must be an object: {path}")
    return value


# ---------------------------------------------------------------- Claude

def _valid_claude_snapshot(value: dict[str, Any]) -> bool:
    return bool(value) and all(isinstance(account, dict) and isinstance(account.get("limits"), list)
                               for account in value.values())


def load_claude_snapshot(
    path: Path = DEFAULT_CLAUDE_SNAPSHOT,
    *,
    refresh: bool = True,
    max_age_seconds: int = CLAUDE_CACHE_SECONDS,
    now: dt.datetime | None = None,
    logins: tuple[list[str], str | None] | None = None,
) -> tuple[dict[str, Any], dt.datetime, str | None]:
    """Load Claude's gauge, re-reading when the cache is older than its TTL.

    `logins` — (every account logged in here, the selected one) — also expires
    the cache the moment it stops describing who is logged in: a /login or a
    `claude-account use` must show the new account's numbers on the next read,
    not up to a TTL later.

    Returns (snapshot, observed_at, warning).

    There is no lock here any more. `claude-account` borrows the access token
    Claude Code already holds and never refreshes it, so a read is a pure reader:
    concurrent callers cannot corrupt anything, and there is nothing to serialize.
    The mutex this used to take was the single most expensive line in the program
    — orphaned once with no holder, it silently served a two-day-old gauge to
    every caller for two days (2026-08-04 → 06). A lock you do not need cannot be
    left behind. The TTL below is politeness to the usage endpoint, nothing more.
    """
    now = now or utc_now()
    cached = path.exists()
    if cached:
        observed_at = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        age = max(0.0, (now - observed_at).total_seconds())
        if not refresh or (age <= max_age_seconds and (
                logins is None or snapshot_matches_logins(read_snapshot(path), *logins))):
            return read_snapshot(path), observed_at, None
    elif not refresh:
        raise PositionError(f"Claude snapshot not found: {path}")

    temporary: Path | None = None
    try:
        executable = shutil.which("claude-account")
        if not executable:
            raise PositionError("claude-account is not on PATH")
        result = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
            raise PositionError(f"claude-account failed: {detail}")
        try:
            snapshot = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PositionError(f"claude-account returned invalid JSON: {exc}") from exc
        if not isinstance(snapshot, dict) or not _valid_claude_snapshot(snapshot):
            raise PositionError("claude-account returned an incomplete snapshot")

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
        observed_at = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        return snapshot, observed_at, None
    except (PositionError, OSError, subprocess.TimeoutExpired) as exc:
        if cached:
            observed_at = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            return read_snapshot(path), observed_at, f"Claude refresh failed ({exc}); showing cached gauge"
        if isinstance(exc, PositionError):
            raise
        raise PositionError(f"Claude refresh failed: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def snapshot_matches_logins(
    snapshot: dict[str, Any], local_emails: list[str], selected_email: str | None,
) -> bool:
    """Whether a cached gauge was read under the logins this machine holds now.

    Pure comparison, no I/O. The selected account must be the one the snapshot
    marked active, and — for snapshots new enough to say (`logged_in`, 2026-09-12)
    — the set of logged-in accounts must be the same set.
    """
    accounts = [account for account in snapshot.values() if isinstance(account, dict)]
    active = {account.get("email") for account in accounts if account.get("active")}
    if selected_email and selected_email not in active:
        return False
    if any("logged_in" in account for account in accounts):
        held = {account.get("email") for account in accounts if account.get("logged_in")}
        if held != set(local_emails):
            return False
    return True


def call_sign(alias: str, email: str | None = None) -> str:
    """The display name for an account: configured by alias or email, else the next NATO letter."""
    name = CLAUDE_CALL_SIGNS.get(alias)
    if name is None and email:
        name = CLAUDE_CALL_SIGNS.get(email) or CLAUDE_CALL_SIGNS.get(email.lower())
        if name is not None:
            CLAUDE_CALL_SIGNS[alias] = name  # so alias-only lookups (the switch log) agree
    if name is None:
        taken = set(CLAUDE_CALL_SIGNS.values())
        name = next((letter for letter in NATO_ALPHABET if letter not in taken), alias)
        CLAUDE_CALL_SIGNS[alias] = name
    return name


def call_sign_order(display: str) -> int:
    """Sort key for Claude rows: configured order first, then NATO order, then the rest."""
    names = list(dict.fromkeys(CLAUDE_CALL_SIGNS.values()))
    if display in names:
        return names.index(display)
    if display in NATO_ALPHABET:
        return len(names) + NATO_ALPHABET.index(display)
    return len(names) + len(NATO_ALPHABET)


def current_login_email(path: Path = CLAUDE_JSON) -> str | None:
    """The email Claude Code is logged in as right now — read straight from config.

    This is ground truth about *who is active*, independent of the (cached) usage
    gauge, so a fresh `claude-account use` is reflected instantly with no API call
    and no token rotation.
    """
    try:
        cfg = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    email = (cfg.get("oauthAccount") or {}).get("emailAddress")
    return email if isinstance(email, str) and email else None


def local_login_emails(
    default_config: Path = CLAUDE_JSON, profiles_dir: Path = CLAUDE_PROFILES_DIR,
) -> list[str]:
    """Every account a login home on this machine holds, the default home first.

    Since 2026-09-12 a satellite can hold more than one Claude login — one per
    config dir — so "logged in here" is a set. Pure file reads, no API.
    """
    configs = [default_config]
    try:
        configs += [entry / ".claude.json" for entry in sorted(profiles_dir.iterdir())
                    if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        pass
    emails: list[str] = []
    for config in configs:
        email = current_login_email(config)
        if email and email not in emails:
            emails.append(email)
    return emails


def selected_login_email(
    selection: Path = CLAUDE_SELECTION, *, logins: list[str] | None = None,
    default_config: Path = CLAUDE_JSON,
) -> str | None:
    """The account new sessions on this machine start in.

    claude-account's selection names it; a selection whose login is gone falls
    back to the default home's account, exactly as the launcher does — so this
    and the account a fresh `claude` actually gets can never disagree.
    """
    logins = local_login_emails(default_config) if logins is None else logins
    try:
        chosen = json.loads(selection.read_text()).get("email")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        chosen = None
    if isinstance(chosen, str) and chosen in logins:
        return chosen
    return current_login_email(default_config)


def reconcile_login(accounts: list[dict[str, Any]], live_email: str | None) -> list[dict[str, Any]]:
    """Override each Claude account's `active` from the live login email.

    The gauge snapshot's own active flag is only as fresh as the snapshot (up to
    CLAUDE_CACHE_SECONDS stale). The login email is not — so right after a switch
    the banner is correct immediately, even on a warm cache. No-op if we can't
    read the config (then the snapshot's flag stands).
    """
    if not live_email:
        return accounts
    for account in accounts:
        if account.get("provider") == "Claude" and account.get("email"):
            account["active"] = account["email"] == live_email
    return accounts


def read_roster(path: Path = CLAUDE_ROSTER) -> dict[str, Any]:
    """claude-account's roster: identity, and since 2026-09-11 dormancy.

    Identity-only, safe to read — there is nowhere in this file to put a
    secret. Missing or malformed is not an error: every account simply reads
    as active, which is the correct default when the file that would say
    otherwise doesn't exist.
    """
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def reconcile_plans(accounts: list[dict[str, Any]], roster: dict[str, Any]) -> list[dict[str, Any]]:
    """Label each Claude account with the plan its own tier says it is on.

    Added the night one account's renewal moved it from Max 20x to Pro.
    Nothing in the usage payload says which plan an account is on; the roster's
    `tier` does, and claude-account refreshes it on every status read. Until this
    existed every Claude account was labelled Max 20x by construction, so a
    downgrade was invisible — and the missing Fable allowance that came with it
    read as an empty cell rather than a plan that no longer includes Fable.
    """
    by_email = {entry.get("email"): entry for entry in roster.values()
                if isinstance(entry, dict) and entry.get("email")}
    for account in accounts:
        if account.get("provider") != "Claude":
            continue
        tier = (by_email.get(account.get("email")) or {}).get("tier")
        if not isinstance(tier, str) or not tier:
            continue
        account["plan"] = CLAUDE_PLAN_BY_TIER.get(tier, tier)
        account["plan_raw"] = tier
    return accounts


def reconcile_dormant(
    accounts: list[dict[str, Any]], roster: dict[str, Any], warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Mark each Claude account dormant per the roster — unless a live login says otherwise.

    When a subscription lapses, claude-account records that (`dormant`,
    `dormant_since`) rather than quietly going on reading it. But a live login outranks a roster snapshot —
    if any satellite is actually logged into the account, someone resubscribed
    it, and inventing dormancy over a login in progress would be the same lie
    roll_forward_windows() refuses to tell about a presumed-fresh window. So
    dormancy is refused whenever `logins` is non-empty, with a warning
    explaining why the row still looks alive. This is the seamless path: log
    back in and every surface un-dims itself with no roster edit at all.
    """
    by_email = {entry.get("email"): entry for entry in roster.values()
                if isinstance(entry, dict) and entry.get("email")}
    for account in accounts:
        if account.get("provider") != "Claude":
            continue
        entry = by_email.get(account.get("email"))
        dormant = bool(entry and entry.get("dormant"))
        if dormant and account.get("logins"):
            dormant = False
            if warnings is not None:
                holders = ", ".join(account["logins"])
                warnings.append(
                    f"{account['display']} is marked dormant in the roster but {holders} "
                    "is logged into it")
        account["dormant"] = dormant
        account["dormant_since"] = (entry.get("dormant_since") if dormant and entry else None)
    return accounts


# ---------------------------------------------------------------- satellites

def query_satellite(host: str, *, timeout: int = SATELLITE_SSH_TIMEOUT) -> dict[str, Any]:
    """Read one remote satellite's beacon over SSH. A pure reader, always.

    The beacon is a file the remote wrote for itself; this only cats it. The read
    cannot be done any other way: macOS refuses to hand a keychain secret to an
    SSH session (`errSecInteractionNotAllowed`), so the gauge must be taken on the
    satellite, inside its GUI session, by its own launchd agent — and published.
    See tools/install-satellite.sh.
    """
    if not SATELLITE_HOST_PATTERN.match(host):
        raise PositionError(f"satellite host {host!r} is not a plain host name")
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
             "-o", "StrictHostKeyChecking=accept-new", "--", host, f"cat {SATELLITE_BEACON_REMOTE}"],
            capture_output=True, text=True, timeout=timeout + 6, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PositionError(f"satellite {host} unreachable: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no beacon"
        raise PositionError(f"satellite {host}: {detail}")
    try:
        beacon = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PositionError(f"satellite {host} beacon is not valid JSON: {exc}") from exc
    if not isinstance(beacon, dict):
        raise PositionError(f"satellite {host} beacon must be an object")
    return beacon


def read_satellites(
    now: dt.datetime, warnings: list[str], *, satellites: Any = REMOTE_SATELLITES,
) -> list[dict[str, Any]]:
    """Every remote satellite's login and gauge, cached and fail-soft.

    A satellite that cannot be reached is never allowed to break the position —
    it drops to whatever beacon was last cached, and past that, out of the read
    entirely with a warning. Losing a satellite must cost that satellite's row, nothing more.
    """
    found: list[dict[str, Any]] = []
    for satellite in satellites:
        name, host = satellite["name"], satellite["host"]
        cache_key = f"satellite-{host}"
        cached = cached_provider_read(cache_key, now, max_age_seconds=SATELLITE_CACHE_SECONDS)
        if cached is not None:
            beacon, observed_at = cached
        else:
            try:
                observed_at = utc_now()
                beacon = query_satellite(host)
                cache_provider_read(cache_key, beacon, observed_at)
            except PositionError as exc:
                stale = cached_provider_read(cache_key, now,
                                             max_age_seconds=SATELLITE_BEACON_MAX_AGE_SECONDS)
                if stale is None:
                    warnings.append(str(exc))
                    continue
                beacon, observed_at = stale
                warnings.append(f"{exc}; showing {name}'s last beacon")
        # The beacon carries its OWN observation time — the moment the satellite
        # read its gauge, not the moment we fetched the file. Ageing a five-minute
        # -old reading by our own clock would understate its freshness; using our
        # clock when the beacon has stopped would overstate it, which is worse.
        taken_at = parse_timestamp(beacon.get("observed_at")) or observed_at
        found.append({
            "name": name,
            "host": host,
            "login_email": beacon.get("login_email") or None,
            "login_emails": [email for email in beacon.get("login_emails") or []
                             if isinstance(email, str) and email],
            "snapshot": beacon.get("snapshot") if isinstance(beacon.get("snapshot"), dict) else {},
            "holds": beacon.get("holds") if isinstance(beacon.get("holds"), dict) else {},
            "observed_at": taken_at,
            "age_seconds": max(0.0, (now - taken_at).total_seconds()),
        })
    return found


def merge_satellite_claude(
    snapshot: dict[str, Any], satellites: list[dict[str, Any]], now: dt.datetime,
    *, warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Fold each satellite's live account into this machine's Claude snapshot.

    Every satellite reads exactly one account live, so between them the fleet can
    see more of the position than any one machine can. The join is on **email**:
    store aliases are per-machine bookkeeping (`work` here, `work-laptop`
    there) and joining on them would silently match nothing.

    A remote reading only ever replaces a local one that is older. That single
    rule is what keeps this safe in both directions — a stale beacon can never
    overwrite a fresh local gauge, and a fresh beacon always wins over a floor
    journaled hours ago.
    """
    by_email = {account.get("email"): alias for alias, account in snapshot.items()
                if isinstance(account, dict) and account.get("email")}
    for satellite in satellites:
        for raw in (satellite.get("snapshot") or {}).values():
            if (not isinstance(raw, dict) or raw.get("stale")
                    or not (raw.get("logged_in") or raw.get("active"))):
                continue  # only an account that satellite holds a login for is a live read
            if not isinstance(raw.get("limits"), list) or not raw["limits"]:
                continue
            alias = by_email.get(raw.get("email"))
            if alias is None:
                if warnings is not None:
                    warnings.append(
                        f"{satellite['name']} is logged into an account this machine has never seen")
                continue
            if satellite["age_seconds"] > SATELLITE_BEACON_MAX_AGE_SECONDS:
                continue  # too old to be a reading; the local journal is no worse
            local = snapshot[alias]
            local_at = parse_timestamp(local.get("fetched_at"))
            if not local.get("stale") and local_at is not None and local_at >= satellite["observed_at"]:
                continue
            local["limits"] = raw["limits"]
            local["stale"] = False
            local["fetched_at"] = iso_utc(satellite["observed_at"])
            local["read_by"] = satellite["name"]
            local.pop("error", None)
    return snapshot


def reconcile_satellite_logins(
    accounts: list[dict[str, Any]], local_emails: str | list[str] | None,
    satellites: list[dict[str, Any]], live_email: str | None = None,
) -> list[dict[str, Any]]:
    """Record, for each Claude account, the satellites logged in to it.

    A satellite can hold several logins (one per login home) but is logged in
    to one: the selected account, the one its new sessions start in. `logins`
    names the satellites whose selected account this is, local first, so every
    surface that says "logged in" says it once per machine. `held_on` names every
    satellite holding a login home for it, whether selected or not, because an
    unselected home can still spend (a one-launch override) and can be picked.

    `live_email` is this machine's selected account; without it, a lone
    `local_emails` string is taken as the selection (the pre-homes call shape).
    A beacon's `login_email` is its selection; `login_emails` its homes.
    """
    local = {local_emails} if isinstance(local_emails, str) else set(local_emails or ())
    if live_email is None and isinstance(local_emails, str):
        live_email = local_emails
    for account in accounts:
        if account.get("provider") != "Claude":
            continue
        email = account.get("email")
        logins: list[str] = []
        held: list[str] = []
        if email and email == live_email:
            logins.append(LOCAL_SATELLITE)
        if email in local or (email and email == live_email):
            held.append(LOCAL_SATELLITE)
        for satellite in satellites:
            selected = satellite.get("login_email")
            homes = satellite.get("login_emails") or ([selected] if selected else [])
            if email and email == selected:
                logins.append(satellite["name"])
            if email in homes:
                held.append(satellite["name"])
        account["logins"] = logins
        account["held_on"] = held
    return accounts


def reconcile_shared_logins(
    accounts: list[dict[str, Any]], provider: str, local_email: str | None,
    satellites: list[dict[str, Any]], warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Record every satellite signed into PROVIDER's single shared account.

    Claude logins come from the beacons' snapshots; a single-account subscription
    publishes its login under the beacon's `holds` instead. Same join key —
    email — and one reading matters: a meter held on more than one machine is
    account-global, so its number already includes every one of those machines'
    burn. A satellite whose login does NOT match is the opposite fact — burn this
    gauge cannot see — and that earns a warning, never a badge.
    """
    for account in accounts:
        if account.get("provider") != provider:
            continue
        names = [LOCAL_SATELLITE] if account.get("active") else []
        for satellite in satellites:
            held = (satellite.get("holds") or {}).get(provider)
            if not held:
                continue
            if local_email and held == local_email:
                names.append(satellite["name"])
            elif local_email and warnings is not None:
                warnings.append(
                    f"{satellite['name']} is signed into a different {provider} account — "
                    "its burn is NOT in these numbers")
        account["logins"] = names
    return accounts


def claude_limits(raw_account: dict[str, Any]) -> list[dict[str, Any]]:
    """The limit rows of one stored Claude account, in the shared limit shape."""
    limits = []
    for raw_limit in raw_account.get("limits", []):
        mapping = CLAUDE_BUCKETS.get(raw_limit.get("label"))
        if not mapping:
            continue
        bucket_id, label, minutes = mapping
        reset = parse_timestamp(raw_limit.get("resets_at"))
        limits.append({
            "meter_id": bucket_id,
            "slot": "primary",
            "label": label,
            "used_percent": float(raw_limit.get("percent") or 0),
            "window_minutes": minutes,
            "resets_at": reset,
            "anchored": reset is not None,
        })
    # 7 DAY, then Fable (a budget inside that same 7 DAY clock), then the 5h
    # throttle — operator ordering 2026-09-12, so the two budgets sit together.
    return sorted(limits, key=lambda limit: CLAUDE_ROW_ORDER.get(limit["meter_id"], 9))


def normalize_claude(snapshot: dict[str, Any], observed_at: dt.datetime) -> list[dict[str, Any]]:
    """Normalize each stored Claude account into the shared account shape."""
    accounts: list[dict[str, Any]] = []
    for alias, raw_account in snapshot.items():
        if not isinstance(raw_account, dict):
            raise PositionError(f"Claude account {alias} is malformed")
        limits = claude_limits(raw_account)
        # A cached-fallback account (claude-account could not read it live) carries
        # its own numbers AND its own observation time — the moment those numbers
        # were last read live, not the moment this snapshot was written. Reporting
        # the snapshot's time for it would age a stale read as if it were fresh,
        # and the views place a five-hour read inside its window by this clock.
        account_observed = parse_timestamp(raw_account.get("fetched_at")) or observed_at
        accounts.append({
            "provider": "Claude",
            "account": alias,
            "email": raw_account.get("email"),
            "display": call_sign(alias, raw_account.get("email")),
            "stale": bool(raw_account.get("stale")),
            "active": bool(raw_account.get("active")),
            "plan": CLAUDE_PLAN,
            "observed_at": min(account_observed, observed_at),
            "limits": limits,
        })
    accounts.sort(key=lambda item: (call_sign_order(item["display"]),
                                    item["display"]))
    return accounts


def keep_claude_state(
    accounts: list[dict[str, Any]], snapshot: dict[str, Any], now: dt.datetime
) -> list[dict[str, Any]]:
    """Persist each account's state while it is readable; serve the newest when it is not.

    An account is only readable live while it is the one logged in. The moment an
    account IS readable is the moment to write its state down: every live read is
    journaled here, per account, and a later lockout is answered with the newest
    state anyone still holds.

    Numbers and reset clocks only — this never sees, stores, or refreshes a
    credential.
    """
    for account in accounts:
        name = f"claude-{account['account']}"
        raw_account = snapshot.get(account["account"])
        if not account.get("stale") and isinstance(raw_account, dict) and account["limits"]:
            cache_provider_read(name, {"limits": raw_account.get("limits", [])},
                                account["observed_at"])
            continue
        # locked out: whichever surviving copy was observed last wins
        kept = cached_provider_read(name, now)
        if kept is None:
            continue
        stored_raw, stored_at = kept
        if stored_at <= account["observed_at"]:
            continue
        limits = claude_limits(stored_raw)
        if limits:
            account["limits"] = limits
            account["observed_at"] = stored_at
    return accounts


# ---------------------------------------------------------------- Codex

def _codex_raw_meters(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    by_id = snapshot.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        return list(by_id.items())
    return [("codex", snapshot.get("rateLimits"))]


def _zero_codex_reset_clocks(snapshot: dict[str, Any]) -> dict[tuple[str, str], int | float]:
    """Return reset clocks for zero-percent windows that can be probed."""
    clocks: dict[tuple[str, str], int | float] = {}
    for map_id, raw_meter in _codex_raw_meters(snapshot):
        if not isinstance(raw_meter, dict):
            continue
        meter_id = raw_meter.get("limitId") or str(map_id)
        for slot in ("primary", "secondary"):
            window = raw_meter.get(slot)
            if not isinstance(window, dict) or window.get("usedPercent") != 0:
                continue
            reset = window.get("resetsAt")
            if isinstance(reset, bool) or not isinstance(reset, int | float):
                continue
            clocks[(meter_id, slot)] = reset
    return clocks


def _mark_codex_zero_anchors(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """Mark fixed zero-percent clocks as active and sliding clocks as unstarted."""
    before_clocks = _zero_codex_reset_clocks(before)
    for map_id, raw_meter in _codex_raw_meters(after):
        if not isinstance(raw_meter, dict):
            continue
        meter_id = raw_meter.get("limitId") or str(map_id)
        for slot in ("primary", "secondary"):
            window = raw_meter.get(slot)
            if not isinstance(window, dict) or window.get("usedPercent") != 0:
                continue
            reset = window.get("resetsAt")
            old_reset = before_clocks.get((meter_id, slot))
            if (
                old_reset is None
                or isinstance(reset, bool)
                or not isinstance(reset, int | float)
            ):
                continue
            window[CODEX_ANCHOR_FIELD] = reset == old_reset
    return after


def query_codex_rate_limits(
    *, timeout_seconds: float = 20.0, executable: str | None = None,
    zero_probe_seconds: float = CODEX_ZERO_PROBE_SECONDS,
) -> dict[str, Any]:
    """Read ``account/rateLimits/read`` from a short-lived app-server.

    The reader runs on its own thread so a silent app-server can only ever time
    out — a blocking readline on the main thread could wedge the whole report.
    A second read distinguishes an active window still rounded to 0% (fixed
    reset clock) from a genuinely unstarted window (sliding reset clock).

    The budget covers the first read; the optional zero-probe extends it rather
    than eating into it. A cold app-server start is slow but not a failure, and
    spending the whole budget on a refinement that only sharpens 0% rows is how
    Codex used to disappear from the position entirely.
    """
    executable = executable or shutil.which("codex")
    if not executable:
        raise PositionError("codex is not on PATH")

    try:
        proc = subprocess.Popen(
            [executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise PositionError(f"could not start codex app-server: {exc}") from exc

    assert proc.stdin is not None and proc.stdout is not None
    incoming: queue.Queue[str | None] = queue.Queue()

    def read_lines() -> None:
        try:
            for line in proc.stdout:
                incoming.put(line)
        finally:
            incoming.put(None)

    reader = threading.Thread(target=read_lines, name="glideslope-codex-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds

    def send(message: dict[str, Any]) -> None:
        try:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PositionError("Codex app-server closed while receiving a request") from exc

    def wait_for(request_id: int) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PositionError("Codex app-server timed out")
            try:
                line = incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise PositionError("Codex app-server timed out") from exc
            if line is None:
                raise PositionError("Codex app-server closed before replying")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                detail = error.get("message", error) if isinstance(error, dict) else error
                raise PositionError(f"Codex app-server error: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise PositionError("Codex app-server returned an invalid result")
            return result

    try:
        send({
            "method": "initialize",
            "id": 0,
            "params": {"clientInfo": {"name": "glideslope", "title": "Glideslope", "version": "0.1.1"}},
        })
        wait_for(0)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 1, "params": None})
        first = wait_for(1)
        if not _zero_codex_reset_clocks(first):
            return first

        probe_delay = max(0.0, zero_probe_seconds)
        time.sleep(probe_delay)
        deadline = time.monotonic() + max(2.0, timeout_seconds / 4)
        try:
            send({"method": "account/rateLimits/read", "id": 2, "params": None})
            second = wait_for(2)
        except PositionError:
            return first  # the first valid snapshot is better than losing Codex entirely
        return _mark_codex_zero_anchors(first, second)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        reader.join(timeout=1)
        proc.stdout.close()


def window_label(duration_minutes: int | None) -> str:
    if duration_minutes == 300:
        return "5h"
    if duration_minutes == 10_080:
        return "Weekly"
    if duration_minutes and duration_minutes % 1_440 == 0:
        return f"{duration_minutes // 1_440}d"
    if duration_minutes and duration_minutes % 60 == 0:
        return f"{duration_minutes // 60}h"
    return f"{duration_minutes}m" if duration_minutes else "Window"


def _numeric_percent(value: Any, meter_id: str, slot: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PositionError(f"Codex meter {meter_id}/{slot} has no numeric usedPercent")
    percent = float(value)
    if not 0 <= percent <= 100:
        raise PositionError(f"Codex meter {meter_id}/{slot} has invalid usedPercent {value}")
    return percent


def _codex_banked_resets(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Full-reset credits Codex has banked. Each one instantly clears the weekly meter.

    The app-server reports these under ``rateLimitResetCredits``; a banked reset
    means the weekly cap is effectively higher on demand. Returns the available
    count and the soonest expiry, or None when none are banked.
    """
    block = snapshot.get("rateLimitResetCredits")
    if not isinstance(block, dict):
        return None
    raw_credits = block.get("credits") if isinstance(block.get("credits"), list) else []
    available = [
        {
            "title": c.get("title") or "Full reset",
            "granted_at": parse_timestamp(c.get("grantedAt")),
            "expires_at": parse_timestamp(c.get("expiresAt")),
        }
        for c in raw_credits
        if isinstance(c, dict) and c.get("status") == "available"
    ]
    # Soonest-expiring first — that credit is the one at risk of lapsing unused.
    far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    available.sort(key=lambda c: c["expires_at"] or far_future)
    raw_count = block.get("availableCount")
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else len(available)
    if count <= 0:
        return None
    soonest = next((c["expires_at"] for c in available if c["expires_at"]), None)
    return {"count": count, "expires_at": soonest, "credits": available}


def normalize_codex(snapshot: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    """Normalize every native Codex meter without assuming a fixed bucket set."""
    raw_meters = _codex_raw_meters(snapshot)

    limits: list[dict[str, Any]] = []
    plan = None
    for map_id, raw_meter in raw_meters:
        if not isinstance(raw_meter, dict):
            raise PositionError(f"Codex meter {map_id} is malformed")
        meter_id = raw_meter.get("limitId") or str(map_id)
        meter_name = raw_meter.get("limitName")
        plan = plan or raw_meter.get("planType")
        for slot in ("primary", "secondary"):
            window = raw_meter.get(slot)
            if window is None:
                continue
            if not isinstance(window, dict):
                raise PositionError(f"Codex meter {meter_id}/{slot} is malformed")
            used = _numeric_percent(window.get("usedPercent"), meter_id, slot)
            duration = window.get("windowDurationMins")
            if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0):
                raise PositionError(f"Codex meter {meter_id}/{slot} has invalid windowDurationMins")
            backend_reset = parse_timestamp(window.get("resetsAt"))
            scope = meter_name or ("all models" if meter_id == "codex" else meter_id)
            # usedPercent is coarse: an active new window can still round to 0%.
            # The live reader probes zero-percent reset clocks and marks whether
            # they are fixed (active) or sliding (not started).
            anchored = used > 0 or window.get(CODEX_ANCHOR_FIELD) is True
            limits.append({
                "meter_id": meter_id,
                "slot": slot,
                "label": f"{window_label(duration)} · {scope}",
                "used_percent": used,
                "window_minutes": duration,
                "resets_at": backend_reset if anchored else None,
                "anchored": anchored,
            })
    if not limits:
        raise PositionError("Codex returned no usable rate-limit windows")
    limits.sort(key=lambda item: (0 if item["meter_id"] == "codex" else 1, item["label"], item["slot"]))
    return {"provider": "Codex", "plan": plan, "observed_at": observed_at, "limits": limits,
            "banked_resets": _codex_banked_resets(snapshot)}


def codex_login_email(path: Path = CODEX_AUTH_JSON) -> str | None:
    """Who this machine's Codex is signed in as — a free local fact, no API call.

    Read from the cached id_token's claims. Only the email claim is decoded;
    the token itself is never held, refreshed, or transmitted by this program.
    """
    try:
        tokens = json.loads(path.read_text()).get("tokens") or {}
        payload = str(tokens.get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (OSError, IndexError, ValueError, AttributeError):
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def codex_accounts(position: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt a Codex position into the shared account shape."""
    return [{
        "provider": "Codex",
        "account": "codex",
        "display": "Codex",
        "stale": False,
        "active": True,
        "plan": CODEX_PLAN,
        "plan_raw": position.get("plan"),
        "observed_at": position["observed_at"],
        "limits": position["limits"],
        "banked_resets": position.get("banked_resets"),
    }]


# ---------------------------------------------------------------- Kimi

def kimi_api_key(keys_file: Path | None = None) -> str | None:
    """The Kimi Code platform key: environment first, then the sanctioned keys file.

    Unlike Claude's OAuth pair this is a static platform key (`sk-kimi-…`) that
    never rotates, so reading it races with nothing and invalidates nothing. The
    keys-file fallback exists for the launchd sampler, which carries no shell
    environment; nothing here writes, refreshes, or copies a credential.
    """
    for name in ("KIMI_API_KEY", "KIMI_CODING_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    try:
        lines = (keys_file or KEYS_FILE).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        for name in ("KIMI_API_KEY=", "KIMI_CODING_API_KEY="):
            if line.startswith(name):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    return None


def query_kimi(*, timeout_seconds: float = 10.0, api_key: str | None = None) -> dict[str, Any]:
    """Read the coding-plan quota from Kimi's usages endpoint. One documented GET."""
    key = api_key or kimi_api_key()
    if not key:
        raise PositionError("Kimi: no KIMI_API_KEY in the environment or keys.txt")
    request = urllib.request.Request(
        KIMI_USAGE_URL, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PositionError(f"Kimi request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PositionError(f"Kimi returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PositionError("Kimi returned no usage object")
    return payload


def _kimi_number(value: Any) -> float | None:
    """Kimi quotes its quota figures as decimal strings; accept numbers too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _kimi_used_percent(detail: dict[str, Any], window: str) -> float:
    """Percent of the window's allowance consumed.

    The figures are normalized quota units (`limit` is 100), not tokens or calls.
    `used` is omitted entirely while it is still zero, so it is derived from
    `remaining` rather than trusted to exist — a missing field must not render as
    a calm 0% when the real answer is unknown.
    """
    limit = _kimi_number(detail.get("limit"))
    if limit is None or limit <= 0:
        raise PositionError(f"Kimi {window} window has no positive numeric limit")
    used = _kimi_number(detail.get("used"))
    if used is None:
        remaining = _kimi_number(detail.get("remaining"))
        if remaining is None:
            raise PositionError(f"Kimi {window} window reports neither used nor remaining")
        used = limit - remaining
    percent = used / limit * 100
    if not -0.001 <= percent <= 100.001:
        raise PositionError(f"Kimi {window} window has out-of-range usage {used} of {limit}")
    return round(max(0.0, min(100.0, percent)), 6)


def _kimi_window_minutes(window: Any) -> int | None:
    """Minutes for a `limits[].window`, translating Kimi's own time-unit enum."""
    if not isinstance(window, dict):
        return None
    duration = _kimi_number(window.get("duration"))
    scale = KIMI_TIME_UNIT_MINUTES.get(str(window.get("timeUnit")))
    if duration is None or duration <= 0 or scale is None:
        return None
    return round(duration * scale)


def normalize_kimi(payload: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    """Normalize the Kimi Code coding plan into the shared position shape.

    Two windows, each a percentage of the plan allowance:
    - the plan quota — the top-level ``usage`` block, a 7-day cycle anchored to
      the subscription date, mapped onto the shared ``weekly_all`` meter so it
      sits beside Claude's and Codex's weekly rows in the hero read.
    - the burst window — the ``limits[]`` entry whose window is 300 minutes.
      Rolling, and it bites even with plan quota left.

    ``limits[]`` is matched by window duration rather than position: the list is
    unordered and may grow, and indexing it would silently mislabel a new window.
    """
    limits: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(meter_id: str, label: str, minutes: int | None, detail: dict[str, Any], window: str) -> None:
        if meter_id in seen:
            return
        reset = parse_timestamp(detail.get("resetTime"))
        seen.add(meter_id)
        limits.append({
            "meter_id": meter_id,
            "slot": "primary",
            "label": label,
            "used_percent": _kimi_used_percent(detail, window),
            "window_minutes": minutes,
            "resets_at": reset,
            "anchored": reset is not None,
        })

    plan_quota = payload.get("usage")
    if isinstance(plan_quota, dict):
        add("weekly_all", "Weekly · all models", KIMI_PLAN_QUOTA_MINUTES, plan_quota, "plan-quota")

    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("detail"), dict):
            continue
        minutes = _kimi_window_minutes(entry.get("window"))
        if minutes == KIMI_SESSION_MINUTES:
            add("session", "5h session", minutes, entry["detail"], "5h")
        elif minutes:
            add(f"window_{minutes}m", f"{window_label(minutes)} · burst", minutes, entry["detail"],
                window_label(minutes))

    if not limits:
        raise PositionError("Kimi returned no usable quota windows")
    # Shortest window first, matching how the Claude buckets are ordered.
    limits.sort(key=lambda item: (item["window_minutes"] or 0, item["label"]))
    membership = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    level = (membership.get("membership") or {}).get("level") if isinstance(membership, dict) else None
    parallel = payload.get("parallel") if isinstance(payload.get("parallel"), dict) else {}
    return {
        "provider": "Kimi",
        "plan": level if isinstance(level, str) else None,
        "observed_at": observed_at,
        "limits": limits,
        "parallel_limit": _kimi_number(parallel.get("limit")),
    }


def kimi_accounts(position: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt a Kimi position into the shared account shape."""
    return [{
        "provider": "Kimi",
        "account": "kimi",
        "display": "Kimi",
        "stale": False,
        "active": True,
        "plan": KIMI_PLAN,
        "plan_raw": position.get("plan"),
        "observed_at": position["observed_at"],
        "limits": position["limits"],
        "parallel_limit": position.get("parallel_limit"),
    }]


# ---------------------------------------------------------------- Grok

def grok_client_version(path: Path | None = None) -> str:
    try:
        payload = json.loads((path or GROK_VERSION_JSON).read_text())
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version else "0.0.0"


def grok_auth_entry(path: Path | None = None) -> tuple[str, dict[str, Any]]:
    """The Grok Build login: one OIDC session in ~/.grok/auth.json.

    Returns (scope_key, entry). The entry holds the access token under `key`;
    callers that only need identity should use grok_login_email() instead.
    """
    auth_path = path or GROK_AUTH_JSON
    try:
        payload = json.loads(auth_path.read_text())
    except FileNotFoundError as exc:
        raise PositionError("Grok: not logged in (no ~/.grok/auth.json)") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionError(f"Grok: could not read auth.json: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise PositionError("Grok: auth.json has no session")
    preferred: tuple[str, dict[str, Any]] | None = None
    first: tuple[str, dict[str, Any]] | None = None
    for scope, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        if first is None:
            first = (scope, entry)
        if entry.get("oidc_issuer") == GROK_OIDC_ISSUER:
            preferred = (scope, entry)
            break
    chosen = preferred or first
    if chosen is None:
        raise PositionError("Grok: auth.json has no session")
    return chosen


def grok_login_email(path: Path | None = None) -> str | None:
    """Who this machine's Grok is signed in as — identity only, no token use.

    Same shape as codex_login_email(): a free local fact for satellite `holds`.
    """
    try:
        _scope, entry = grok_auth_entry(path)
    except PositionError:
        return None
    email = entry.get("email")
    return email if isinstance(email, str) and email else None


# A failed refresh is not retried on the sampler's clock: one bad refresh token
# would otherwise mean an authenticated failure against the issuer every minute.
GROK_REFRESH_BACKOFF_SECONDS = 3600
GROK_REFRESH_FAILURE_PATH = STORE_DIR / "grok-refresh-failed.json"


def note_grok_refresh_failure(now: dt.datetime, reason: str, path: Path | None = None) -> None:
    """Remember that a refresh failed, so the next ticks hold off. Never raises."""
    target = path or GROK_REFRESH_FAILURE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"failed_at": iso_utc(now), "reason": reason[:300]}))
        os.chmod(target, 0o600)
    except OSError:
        pass


def clear_grok_refresh_failure(path: Path | None = None) -> None:
    try:
        (path or GROK_REFRESH_FAILURE_PATH).unlink()
    except OSError:
        pass


def grok_refresh_backoff(now: dt.datetime, path: Path | None = None,
                         *, seconds: int = GROK_REFRESH_BACKOFF_SECONDS) -> str | None:
    """A short description of the failure still being held against, or None."""
    try:
        record = json.loads((path or GROK_REFRESH_FAILURE_PATH).read_text())
    except (OSError, ValueError):
        return None
    failed_at = parse_timestamp(record.get("failed_at")) if isinstance(record, dict) else None
    if failed_at is None or (now - failed_at).total_seconds() >= seconds:
        return None
    return f"{format_countdown(now - failed_at)} ago: {record.get('reason', 'unknown')}"


def _grok_token_expired(entry: dict[str, Any], now: dt.datetime) -> bool:
    expires_at = parse_timestamp(entry.get("expires_at"))
    if expires_at is None:
        return True
    return expires_at.timestamp() <= now.timestamp() + GROK_TOKEN_SKEW_SECONDS


def _grok_discover_token_endpoint(issuer: str, *, timeout_seconds: float) -> str:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return GROK_TOKEN_ENDPOINT
    endpoint = payload.get("token_endpoint") if isinstance(payload, dict) else None
    return endpoint if isinstance(endpoint, str) and endpoint else GROK_TOKEN_ENDPOINT


def _grok_refresh_tokens(
    entry: dict[str, Any], now: dt.datetime, *, timeout_seconds: float
) -> dict[str, Any]:
    """OIDC refresh_token grant — the same operation Grok's own client runs.

    Only against the first-party xAI issuer. A custom/team IdP is not something
    this program will drive headlessly.
    """
    issuer = entry.get("oidc_issuer") or ""
    refresh_token = entry.get("refresh_token") or ""
    client_id = entry.get("oidc_client_id") or ""
    if issuer != GROK_OIDC_ISSUER or not refresh_token or not client_id:
        raise PositionError("Grok: session expired — run `grok login`")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        **({k: entry[k] for k in ("principal_type", "principal_id")
            if isinstance(entry.get(k), str) and entry.get(k)}),
    }).encode()
    endpoint = _grok_discover_token_endpoint(issuer, timeout_seconds=timeout_seconds)
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PositionError(f"Grok token refresh failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PositionError(f"Grok token refresh returned invalid JSON: {exc}") from exc
    access = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access, str) or not access:
        raise PositionError("Grok token refresh returned no access_token")
    updated = dict(entry)
    updated["key"] = access
    new_refresh = payload.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        updated["refresh_token"] = new_refresh
    expires_in = payload.get("expires_in")
    if not (isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0):
        raise PositionError("Grok token refresh returned no expires_in; not adopting the token")
    updated["expires_at"] = iso_utc(now + dt.timedelta(seconds=float(expires_in)))
    return updated


def _grok_persist_entry(auth_path: Path, scope: str, entry: dict[str, Any]) -> None:
    """Write the refreshed session back into auth.json, atomically, under flock.

    Re-reads under the lock so a Grok process that refreshed in the same window
    is not clobbered. Only the touched session's token fields change.
    """
    lock_path = Path(str(auth_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            payload = json.loads(auth_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # A file we cannot read (a Grok write in flight, say) must not be
            # replaced by one that holds only our three fields: that logs Grok out.
            raise PositionError(f"Grok: auth.json unreadable under lock; refresh not written: {exc}") from exc
        if not isinstance(payload, dict):
            raise PositionError("Grok: auth.json is not an object; refresh not written")
        current = payload.get(scope) if isinstance(payload.get(scope), dict) else {}
        merged = dict(current)
        merged.update({k: entry[k] for k in ("key", "refresh_token", "expires_at") if k in entry})
        payload[scope] = merged
        directory = auth_path.parent
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=f".{auth_path.name}.", delete=False
        ) as handle:
            json.dump(payload, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, auth_path)


def grok_access_token(
    path: Path | None = None, *, now: dt.datetime | None = None, timeout_seconds: float = 10.0
) -> tuple[str, str]:
    """Return (access_token, user_id), refreshing the Grok login in place if needed."""
    auth_path = path or GROK_AUTH_JSON
    now = now or utc_now()
    scope, entry = grok_auth_entry(auth_path)
    if _grok_token_expired(entry, now):
        held = grok_refresh_backoff(now)
        if held:
            raise PositionError(f"Grok: last token refresh failed ({held}); not retrying yet — run `grok login` if it persists")
        try:
            entry = _grok_refresh_tokens(entry, now, timeout_seconds=timeout_seconds)
            _grok_persist_entry(auth_path, scope, entry)
        except PositionError as exc:
            note_grok_refresh_failure(now, str(exc))
            raise
        except OSError as exc:
            note_grok_refresh_failure(now, str(exc))
            raise PositionError(f"Grok: could not persist refreshed session: {exc}") from exc
        clear_grok_refresh_failure()
    token = entry.get("key")
    user_id = entry.get("user_id") or entry.get("principal_id")
    if not isinstance(token, str) or not token:
        raise PositionError("Grok: auth.json has no access token")
    if not isinstance(user_id, str) or not user_id:
        raise PositionError("Grok: auth.json has no user_id")
    return token, user_id


def read_grok_hook_snapshot(
    path: Path | None = None, *, now: dt.datetime | None = None,
    max_age_seconds: int = GROK_HOOK_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    """A numbers-only billing snapshot written by the Grok Stop/SessionStart hook."""
    snapshot_path = path or GROK_HOOK_SNAPSHOT
    now = now or utc_now()
    try:
        payload = json.loads(snapshot_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot"), dict):
        return None
    observed_at = parse_timestamp(payload.get("observed_at"))
    if observed_at is None or (now - observed_at).total_seconds() > max_age_seconds:
        return None
    return payload["snapshot"]


def write_grok_hook_snapshot(
    raw: dict[str, Any], observed_at: dt.datetime, path: Path | None = None
) -> None:
    """Persist a billing payload the sampler can prefer to a live GET. Never raises."""
    snapshot_path = path or GROK_HOOK_SNAPSHOT
    try:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(snapshot_path.parent, 0o700)
        with tempfile.NamedTemporaryFile(
            "w", dir=snapshot_path.parent, prefix=f".{snapshot_path.name}.", delete=False
        ) as handle:
            json.dump({"observed_at": iso_utc(observed_at), "snapshot": raw}, handle,
                      separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, snapshot_path)
    except OSError:
        pass


def _grok_billing_get(
    access_token: str, user_id: str, *, timeout_seconds: float, version: str
) -> dict[str, Any]:
    request = urllib.request.Request(
        GROK_BILLING_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-userid": user_id,
            "x-grok-client-version": version,
            "x-grok-client-mode": "headless",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PositionError(f"Grok billing request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PositionError(f"Grok billing returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PositionError("Grok billing returned no object")
    return payload


def query_grok(
    *,
    timeout_seconds: float = 10.0,
    auth_path: Path | None = None,
    snapshot_path: Path | None = None,
    force_live: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read SuperGrok's weekly pool. Prefers a fresh hook snapshot; else a live GET.

    `force_live` is for the hook itself, so writing a snapshot cannot recurse
    into the file it is about to replace.
    """
    now = now or utc_now()
    if not force_live:
        snapshot = read_grok_hook_snapshot(snapshot_path, now=now)
        if snapshot is not None:
            return snapshot
    token, user_id = grok_access_token(auth_path, now=now, timeout_seconds=timeout_seconds)
    return _grok_billing_get(
        token, user_id, timeout_seconds=timeout_seconds, version=grok_client_version())


def _grok_percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _grok_period_minutes(config: dict[str, Any]) -> tuple[dt.datetime | None, int]:
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    start = parse_timestamp(period.get("start") or config.get("billingPeriodStart"))
    end = parse_timestamp(period.get("end") or config.get("billingPeriodEnd"))
    if start is not None and end is not None and end > start:
        return end, max(1, round((end - start).total_seconds() / 60))
    return end, GROK_WEEKLY_MINUTES


def normalize_grok(payload: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    """Normalize SuperGrok's shared weekly pool into the shared position shape.

    One window today: ``creditUsagePercent`` over ``USAGE_PERIOD_TYPE_WEEKLY``,
    mapped onto ``weekly_all`` so it sits in the hero read next to Claude, Codex
    and Kimi. There is no 5h session in the payload xAI returns; if a later
    period of 300 minutes appears it is picked up as ``session`` by duration,
    never invented.
    """
    config = payload.get("config") if isinstance(payload.get("config"), dict) else None
    if config is None:
        raise PositionError("Grok billing returned no config")
    percent = _grok_percent(config.get("creditUsagePercent"))
    if percent is None:
        raise PositionError("Grok billing has no creditUsagePercent")
    if not -0.001 <= percent <= 100.001:
        raise PositionError(f"Grok weekly usage is out of range: {percent}")
    reset, minutes = _grok_period_minutes(config)
    limits: list[dict[str, Any]] = [{
        "meter_id": "weekly_all" if minutes != 300 else "session",
        "slot": "primary",
        "label": "Weekly · all models" if minutes != 300 else "5h session",
        "used_percent": round(max(0.0, min(100.0, percent)), 6),
        "window_minutes": minutes,
        "resets_at": reset,
        "anchored": reset is not None,
    }]
    products: list[dict[str, Any]] = []
    for entry in config.get("productUsage") or []:
        if not isinstance(entry, dict):
            continue
        product = entry.get("product")
        used = _grok_percent(entry.get("usagePercent"))
        if isinstance(product, str) and used is not None:
            products.append({"product": product, "used_percent": used})
    plan_raw = payload.get("subscriptionTier") or config.get("subscriptionTier")
    return {
        "provider": "Grok",
        "plan": plan_raw if isinstance(plan_raw, str) else None,
        "observed_at": observed_at,
        "limits": limits,
        "product_usage": products,
    }


def grok_accounts(position: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt a Grok position into the shared account shape."""
    return [{
        "provider": "Grok",
        "account": "grok",
        "display": "Grok",
        "stale": False,
        "active": True,
        "plan": GROK_PLAN,
        "plan_raw": position.get("plan"),
        "observed_at": position["observed_at"],
        "limits": position["limits"],
        "product_usage": position.get("product_usage") or [],
    }]


# ---------------------------------------------------------------- OpenRouter

def query_openrouter(*, timeout_seconds: float = 10.0, api_key: str | None = None) -> dict[str, Any]:
    """Read this key's usage from OpenRouter's /key endpoint. Never touches secrets on disk."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise PositionError("OPENROUTER_API_KEY is not set")
    request = urllib.request.Request(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PositionError(f"OpenRouter request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PositionError(f"OpenRouter returned invalid JSON: {exc}") from exc
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PositionError("OpenRouter returned no key data")
    return data


def _usd(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_openrouter(data: dict[str, Any], observed_at: dt.datetime) -> dict[str, Any]:
    """OpenRouter reports rolling dollar burn natively; usage_weekly is the last 7 days."""
    weekly = _usd(data.get("usage_weekly"))
    if weekly is None:
        raise PositionError("OpenRouter response has no numeric usage_weekly")
    return {
        "provider": "OpenRouter",
        "display": "OpenRouter",
        "weekly_usd": weekly,
        "limit_usd": _usd(data.get("limit")),
        "remaining_usd": _usd(data.get("limit_remaining")),
        "limit_reset": data.get("limit_reset"),
        "observed_at": observed_at,
    }


# ------------------------------------------------------- harness-spend (local)

# List price per 1M tokens (input, output, cache-read) for the "≈ API" column ONLY.
# This is a shadow estimate of what subscription work would have cost billed per-token —
# never a position, never reconciled against a gauge. Position always comes from the
# provider's own meter (see PROVIDERS.md). Read from openrouter.ai/api/v1/models
# 2026-08-06; a model absent here renders "—" rather than guessing.
SHADOW_PRICES = {
    # Kimi K3. Harnesses spell the same model differently (kimi-code qualifies it,
    # opencode/pi/omp do not), so both spellings map to the one price.
    "kimi-code/k3-256k": (3.00, 15.00, 0.30),
    "k3-256k": (3.00, 15.00, 0.30),
}

# Rows below this many total tokens are dropped from the subscription table — they are
# stray one-off calls, not work. Anything dropped is counted in a note, never silently.
SUBSCRIPTION_ROW_FLOOR = 1_000_000

# A harness whose newest row is older than this is called out. The collector logs only
# on import, so correct silence and a dead collector look identical without this.
STALE_IMPORT_HOURS = 24


def shadow_cost(model: str | None, fresh: int, output: int, cache_read: int) -> float | None:
    """What this token flow would have cost at list price, or None if the model is unpriced."""
    price = SHADOW_PRICES.get(model or "")
    if price is None:
        return None
    per_in, per_out, per_cache = price
    return (fresh * per_in + output * per_out + cache_read * per_cache) / 1_000_000


def _harness_host() -> str:
    """Same host id the collector writes under (lowercase LocalHostName)."""
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"],
                             capture_output=True, text=True, timeout=5)
        if out.stdout.strip():
            return out.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0].lower()


def _et_day_bucket(ts_utc: dt.datetime) -> str:
    """ET day, rolling at 6 AM — mirrors harness-spend/store.day_bucket."""
    ts_et = ts_utc.astimezone(LOCAL_TZ)
    if ts_et.hour < 6:
        ts_et -= dt.timedelta(days=1)
    return ts_et.strftime("%Y-%m-%d")


def query_harness_spend(now: dt.datetime, *, db: Path | None = None) -> dict[str, Any] | None:
    """Local OpenRouter attribution from this host's telemetry DB.

    Read-only and fully failure-isolated: any problem (missing DB, locked file,
    schema drift) returns None so the Claude/Codex position never breaks.
    """
    path = db or (HARNESS_SPEND_DIR / f"telemetry-{_harness_host()}.db")
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            def total(floor: str) -> float:
                row = conn.execute(
                    "SELECT SUM(cost) c FROM requests "
                    "WHERE provider = 'openrouter' AND day_bucket >= ?",
                    (floor,),
                ).fetchone()
                return row["c"] or 0.0

            today = _et_day_bucket(now)
            month_floor = _et_day_bucket(now - dt.timedelta(days=30))
            by_h = conn.execute(
                "SELECT harness, SUM(cost) c FROM requests "
                "WHERE provider = 'openrouter' AND day_bucket >= ? "
                "GROUP BY harness ORDER BY c DESC",
                (today,),
            ).fetchall()
            # Newest row of any harness — the difference between "nothing was spent"
            # and "nothing was collected", which the log alone cannot tell you.
            latest = conn.execute("SELECT MAX(ts) t FROM requests").fetchone()["t"]
            # Subscription work: flat-rate harnesses bill no marginal cost, so the
            # tokens are the whole signal. Cache efficiency is what makes them cheap.
            subs = conn.execute(
                "SELECT harness, model, COUNT(*) calls, "
                "  SUM(COALESCE(cache_read_tokens,0)) cache_read, "
                "  SUM(COALESCE(input_tokens,0)) fresh, "
                "  SUM(COALESCE(output_tokens,0)) out "
                "FROM requests WHERE COALESCE(cost,0) = 0 AND day_bucket >= ? "
                "GROUP BY harness, model "
                "HAVING cache_read + fresh + out > 0 "
                "ORDER BY cache_read + fresh + out DESC",
                (month_floor,),
            ).fetchall()
            return {
                "host": _harness_host(),
                "today": total(today),
                "week": total(_et_day_bucket(now - dt.timedelta(days=7))),
                "month": total(_et_day_bucket(now - dt.timedelta(days=30))),
                "harness_today": [(r["harness"], r["c"] or 0.0) for r in by_h],
                "latest_ts": parse_timestamp(latest),
                "subscription": [dict(r) for r in subs],
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


# ---------------------------------------------------------------- presentation

def roll_forward_windows(accounts: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    """Advance every window whose reset instant has already passed.

    A gauge read before its window ended describes a window that no longer
    exists. The renderers used to give up on both facts at once and print
    "stale · reset passed", which threw away two things this program knows.

    The clock is arithmetic. These windows have a fixed period, so the next
    reset is the recorded one advanced by whole periods — knowable no matter how
    old the read is, and never withheld again.

    The percent is a different kind of claim. The provider zeroes the meter at
    the roll, so the new window *started* at 0; but only "nobody has burned into
    it since" makes 0 the value NOW, and that holds for exactly one case: a
    Claude account this host is not logged into, which Claude Code cannot have
    spent. Even there it stays a presumption, not a reading — the quota is per
    subscription, so claude.ai, the phone, or another machine are invisible from
    here — so it is marked `presumed` and every surface must carry the marker.

    For anything still in use (the active Claude account, Codex, Kimi) the new
    window's burn is genuinely unknown, and the percent is dropped rather than
    guessed. Presuming 0% for a meter someone is actively spending would invent
    headroom, which is the one lie this instrument must never tell.

    A dormant account (ruling 2026-09-11) fails the same test a different way:
    it has no subscription to have a position on at all, so presuming it fresh
    would invent a window rather than a percent. It gets the unread treatment —
    a known clock, no number — never the presumed-zero one.
    """
    for account in accounts:
        # Burn requires a client. Only a Claude account NO satellite is logged
        # into is one the fleet provably cannot have spent since the roll — the
        # test used to be "not logged in here", which quietly became a lie the
        # day a second machine held a login of its own.
        held = account.get("held_on", account.get("logins"))
        in_use = bool(held) if held is not None else bool(account.get("active"))
        presumable = (account.get("provider") == "Claude" and not in_use
                     and not account.get("dormant"))
        for limit in account.get("limits", []):
            reset = limit.get("resets_at")
            period_minutes = limit.get("window_minutes")
            if not isinstance(reset, dt.datetime) or not period_minutes or reset > now:
                continue
            period = dt.timedelta(minutes=period_minutes)
            periods = int((now - reset) / period) + 1
            limit["observed_percent"] = limit.get("used_percent")
            limit["observed_resets_at"] = reset
            limit["rolled_periods"] = periods
            limit["resets_at"] = reset + period * periods
            limit["used_percent"] = 0.0 if presumable else None
            limit["presumed"] = presumable
    return accounts


def even_pace_percent(limit: dict[str, Any], now: dt.datetime) -> float | None:
    """Where even burn would put you: the share of the window already elapsed."""
    reset = limit.get("resets_at")
    duration = limit.get("window_minutes")
    if not isinstance(reset, dt.datetime) or not duration:
        return None
    remaining = (reset - now).total_seconds()
    if remaining < 0:
        return None
    elapsed = 1 - remaining / (float(duration) * 60)
    return round(max(0.0, min(100.0, elapsed * 100)), 6)


# ANSI styling for direct-terminal viewing. It is threaded through the renderers
# as a bool and only turned on for a real TTY (see color_enabled), so output that
# an agent captures or pipes stays clean, relay-ready markdown.
ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m", "cyan": "\033[36m",
}


def color_enabled(mode: str, stream: Any = None) -> bool:
    """Resolve --color {auto,always,never} against the stream and NO_COLOR."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:  # https://no-color.org
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, *styles: str, on: bool) -> str:
    if not on or not styles:
        return text
    return "".join(ANSI[s] for s in styles) + text + ANSI["reset"]


def used_style(used_percent: float) -> str:
    """Traffic light on proximity to the cap: green has runway, red is at the wall."""
    if used_percent >= 90:
        return "red"
    if used_percent >= 75:
        return "yellow"
    return "green"


def heading(text: str, *, color: bool) -> str:
    """A section title: ANSI-bold for the terminal, **markdown-bold** for relay."""
    return paint(text, "bold", on=True) if color else f"**{text}**"


def format_percent(value: float) -> str:
    return f"{value:.0f}%" if value == round(value) else f"{value:.1f}%"


def format_countdown(delta: dt.timedelta) -> str:
    """A human countdown in its two largest units: '6d 5h', '3h 24m', '45m'.

    Raw decimal hours ('149.7h') are hard to read at a glance; days-then-hours
    is what the eye actually wants for the multi-day weekly windows.
    """
    total_minutes = max(0, round(delta.total_seconds() / 60))
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_reset(limit: dict[str, Any], now: dt.datetime) -> str:
    reset = limit.get("resets_at")
    if not isinstance(reset, dt.datetime):
        return "not started"
    if reset < now:
        return "stale · reset passed"
    date = reset.astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p %Z")
    return f"{format_countdown(reset - now)} – {date}"


def format_used(used: str, pace: float | None, *, color: bool = False, level: float | None = None) -> str:
    """The used-column cell: the used figure, then the ◆ Glideslope mark folded in.

    `level` is the numeric used-percent used only to pick the traffic-light color;
    pass None (e.g. a dollar figure) to leave the figure uncolored.
    """
    if color and level is not None:
        used = paint(used, used_style(level), "bold", on=True)
    if pace is None:
        return used
    return f"{used} {paint(f'(◆ {format_percent(pace)})', 'dim', on=color)}"


def used_cell(account: dict[str, Any], limit: dict[str, Any], now: dt.datetime, *, color: bool) -> str:
    """The Used cell for one window, honest about which kind of number it holds.

    Three kinds, and they must never look alike: a reading, a presumption about
    a window that rolled while nobody was watching, and an admission that the
    burn is unknown.
    """
    percent = limit.get("used_percent")
    if percent is None:
        # a window that rolled since the last read, on a meter still in use:
        # the clock is known (it is in the reset column), the burn is not
        return paint("unread", "dim", on=color)
    presumed = bool(limit.get("presumed"))
    # A stale account's numbers are a floor, not a position — pacing them against
    # the clock would read as current truth. A presumed-fresh window is the one
    # exception: its clock IS this window's, so the mark is honest.
    pace = None if (account.get("stale") and not presumed) else even_pace_percent(limit, now)
    text = format_percent(percent) + (" presumed" if presumed else "")
    return format_used(text, pace, color=color, level=percent)


def account_name(account: dict[str, Any]) -> str:
    name = str(account.get("display") or account.get("account"))
    # Login is a place, not a boolean: an account is logged in ON a named
    # satellite, and more than one satellite can hold one. Any account that
    # knows its holders wears the marks — ● here, ◦ elsewhere — so a shared
    # meter (Codex on every machine) reads as shared at a glance.
    logins = account.get("logins")
    if logins:
        for satellite in logins:
            name += f" ● {satellite}" if satellite == LOCAL_SATELLITE else f" ◦ {satellite}"
    elif account.get("provider") == "Claude" and account.get("active"):
        name += " · active"
    if account.get("dormant"):
        name += " · dormant"
    if account.get("stale"):
        name += " · stale"
    if account.get("plan"):
        name += f" · {account['plan']}"
    return name


# Meter badges: the outline says which window, the letter inside says whose.
# Circle = weekly (all models), triangle = 5h session, diamond = weekly Fable —
# the same vocabulary the popup's plot and register draw.
# Codex takes X because C is the third Claude account's.
BADGE_LETTERS = {"Alpha": "A", "Bravo": "B", "Charlie": "C", "Delta": "D", "Codex": "X", "Kimi": "K", "Grok": "G"}
BADGE_ENCLOSURES = {"circle": "⃝", "diamond": "⃟", "triangle": "⃤"}


def meter_badge(account: dict[str, Any], limit: dict[str, Any]) -> str:
    """A letter inside an outline shape: the account, enclosed by its window kind."""
    display = str(account.get("display") or account.get("account") or "?")
    letter = BADGE_LETTERS.get(display, display[:1].upper())
    if limit.get("meter_id") == "weekly_fable" or limit.get("label") == "Weekly · Fable":
        shape = "diamond"
    elif limit.get("meter_id") == "session" or limit.get("window_minutes") == 300:
        shape = "triangle"
    else:
        shape = "circle"
    return letter + BADGE_ENCLOSURES[shape]


def held_elsewhere(account: dict[str, Any]) -> bool:
    """True when every machine holding this account is some OTHER satellite.

    That burn is real and belongs in the pool, but nothing typed at this
    keyboard drives it — so its rows dress down (italic in relay, dim in the
    terminal) and the reader can split fleet burn from their own at a glance.
    """
    logins = account.get("logins")
    return bool(logins) and LOCAL_SATELLITE not in logins


def _elsewhere_name(name: str, *, color: bool) -> str:
    """The account cell of a row whose burn happens on another machine."""
    return paint(name, "dim", on=True) if color else f"*{name}*"


def _bold(text: str, *, color: bool) -> str:
    """Bold for either channel: ANSI in the terminal, **markdown** for relay."""
    return paint(text, "bold", on=True) if color else f"**{text}**"


def banner_table(title: str, headers: list[str], rows: list[list[str]], *, color: bool = False) -> list[str]:
    """A GFM table led by a full-width, centered banner title row.

    The banner is the table's header row spanned across every column with the
    `| title |||` colspan form; the column labels sit in the first body row
    (bold); every column is centered so the banner reads centered in renderers
    that honor the span. Returns the table's lines (no surrounding blanks).
    """
    n = len(headers)
    banner = f"| {paint(title, 'bold', on=color)} " + "|" * n  # colspan across all n columns
    delimiter = "|" + "|".join([":-:"] * n) + "|"
    labels = "| " + " | ".join(_bold(header, color=color) for header in headers) + " |"
    lines = [banner, delimiter, labels]
    lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    return lines


def render_markdown(accounts: list[dict[str, Any]], now: dt.datetime, warnings: list[str], *, color: bool = False) -> str:
    headers = ["Provider", "Account", "Window", "Used", "Next reset at"]
    rows: list[list[str]] = []
    for account in accounts:
        if account.get("dormant"):
            continue  # a dead account's windows are noise; it keeps one row in Weekly status only
        name = account_name(account)
        if held_elsewhere(account):
            name = _elsewhere_name(name, color=color)
        for limit in account.get("limits", []):
            used = f"{meter_badge(account, limit)} {used_cell(account, limit, now, color=color)}"
            reset = paint(format_reset(limit, now), "dim", on=color)
            rows.append([account["provider"], name, limit["label"], used, reset])
    if not rows:
        rows.append(["—", "—", "no usage data", "—", "—"])
    lines = banner_table("All windows", headers, rows, color=color)
    if warnings:
        lines.append("")
        lines.extend(f"> {warning}" for warning in warnings)
    return "\n".join(lines)


def _weekly_all(account: dict[str, Any]) -> dict[str, Any] | None:
    return next((limit for limit in account.get("limits", []) if limit.get("label") == "Weekly · all models"), None)


def _weekly_fable(account: dict[str, Any]) -> dict[str, Any] | None:
    """The Fable weekly, when this account has one — Claude only, today.

    Matched by label like `_weekly_all`, not `meter_id`, for the same reason:
    a future provider could reuse `weekly_fable` for something of its own, and
    the label is the actual contract with the hero table's Fable column.
    """
    return next((limit for limit in account.get("limits", []) if limit.get("label") == "Weekly · Fable"), None)


def exhausts_at(limit: dict[str, Any], now: dt.datetime) -> dt.datetime | None:
    """When this window runs out if the rate it has burned so far continues.

    None when it does not run out before its own reset — a different and
    perfectly good outcome, not a missing value. Same arithmetic the approach
    plot's flight path draws, kept here so the CLI, the notifier and the views
    can never disagree about when a window dies.
    """
    reset = limit.get("resets_at")
    percent = limit.get("used_percent")
    duration = limit.get("window_minutes")
    if not isinstance(reset, dt.datetime) or not duration or reset <= now:
        return None
    if percent is None or percent <= 0 or percent >= 100:
        return None
    start = reset - dt.timedelta(minutes=duration)
    elapsed = (now - start).total_seconds()
    if elapsed <= 0:
        return None
    at = now + dt.timedelta(seconds=(100 - percent) / (percent / elapsed))
    return at if at < reset else None


def notifiable_windows(
    accounts: list[dict[str, Any]], now: dt.datetime, *,
    threshold: float = NOTIFY_PERCENT, satellite: str | None = None,
) -> list[dict[str, Any]]:
    """Windows on THIS machine's logged-in account that have crossed the line.

    Deliberately local-only. A notification is for the person sitting at this
    keyboard, and the only account they can act on without a web login is the one
    they are on; an account held by another satellite crossing 90% is news nobody
    here can use, and the satellite holding it has no one looking at its screen.

    Only a reading counts. A presumed zero cannot cross a threshold, and an
    unread window has no number to compare — but a **stale** one may, because a
    stale percent is a floor and a floor at or above the line is above the line.
    """
    watching = satellite or LOCAL_SATELLITE
    alerts: list[dict[str, Any]] = []
    for account in accounts:
        # A dormant account and a login on it are mutually exclusive in
        # practice (live-login-wins undoes the dormant flag) — checked anyway,
        # because "no position to warn about" should never depend on that.
        if (account.get("provider") != "Claude" or account.get("dormant")
                or watching not in (account.get("logins") or [])):
            continue
        for limit in account.get("limits", []):
            if limit.get("meter_id") not in NOTIFY_METERS or limit.get("presumed"):
                continue
            percent = limit.get("used_percent")
            reset = parse_timestamp(limit.get("resets_at"))
            if percent is None or float(percent) < threshold or reset is None or reset <= now:
                continue
            alerts.append({
                "account": account.get("display"),
                # the store alias too: the sample DB is keyed by it, not by call-sign
                "alias": account.get("account"),
                "provider": account.get("provider"),
                "satellite": watching,
                "meter_id": limit.get("meter_id"),
                "label": limit.get("label"),
                "used_percent": float(percent),
                "resets_at": reset,
                "window_minutes": limit.get("window_minutes"),
                "exhausts_at": exhausts_at({**limit, "resets_at": reset}, now),
                "floor": bool(account.get("stale")),
            })
    return alerts


def best_alternative(
    accounts: list[dict[str, Any]], now: dt.datetime, *, exclude: str | None,
    threshold: float = NOTIFY_PERCENT,
) -> dict[str, Any] | None:
    """The account with the most weekly room left — the one to swap to.

    Ranked by how far BELOW its own even-burn mark it sits, not by raw percent:
    an account at 40% one day into its week is closer to the wall than one at
    40% on day six. An account already over the line itself is no refuge and is
    never offered — checked on BOTH weeklies, because a Fable-capped account is
    just as much "no refuge" as an all-models-capped one, even if the meter that
    would rank it is the other.

    An account carries two weekly budgets, and Fable is usually the one that
    binds — the whole reason it exists as a separate mark (PROVIDERS.md). So the
    account's own ranking figure is its BINDING weekly: whichever of the two
    sits on less slack, i.e. closer to running out relative to its own clock.
    The returned dict names which one bound as `meter_id`, so a caller (notify's
    message) can say so.
    """
    best: dict[str, Any] | None = None
    for account in accounts:
        if account.get("provider") != "Claude" or account.get("display") == exclude or account.get("dormant"):
            continue
        binding: dict[str, Any] | None = None
        disqualified = False
        for meter_id, weekly in (("weekly_all", _weekly_all(account)), ("weekly_fable", _weekly_fable(account))):
            if weekly is None:
                continue
            # Tolerate both shapes: the in-process account carries datetimes, the
            # --json payload the sampler holds carries ISO strings. A silent
            # None here would just quietly stop offering anywhere to swap to.
            percent = weekly.get("used_percent")
            pace = even_pace_percent(
                {**weekly, "resets_at": parse_timestamp(weekly.get("resets_at"))}, now)
            if percent is None or pace is None:
                continue
            if float(percent) >= threshold:
                disqualified = True  # over the line on EITHER weekly is no refuge
                break
            slack = pace - float(percent)
            if binding is None or slack < binding["slack"]:
                binding = {"meter_id": meter_id, "used_percent": float(percent),
                          "pace_percent": pace, "slack": slack}
        if disqualified or binding is None:
            continue
        if best is None or binding["slack"] > best["slack"]:
            best = {"display": account.get("display"), "used_percent": binding["used_percent"],
                    "pace_percent": binding["pace_percent"], "slack": binding["slack"],
                    "meter_id": binding["meter_id"], "floor": bool(account.get("stale"))}
    return best


PICK_METER_LABELS = {"session": "5h session", "weekly_all": "weekly", "weekly_fable": "Fable weekly"}


def pick_account(
    accounts: list[dict[str, Any]], now: dt.datetime, *, threshold: float = NOTIFY_PERCENT,
) -> dict[str, Any]:
    """Which Claude account new sessions on this machine should start in. Sticky.

    Stay on the selected account while every one of its meters — the 5h session
    and both weeklies — sits below the line. Moving costs something real: a
    resumed conversation rebuilds its whole prompt cache on the new account. Past
    the line, move to best_alternative() among the accounts this machine holds a
    login for (a pick you cannot launch into is no pick), excluding any whose own
    5h session is already over. With no refuge anywhere, stay and say so.

    Returns {email, display, account, stay, reason}; email is None only when this
    machine holds no usable Claude login at all.
    """
    local = [account for account in accounts
             if account.get("provider") == "Claude" and not account.get("dormant")
             and LOCAL_SATELLITE in (account.get("held_on") or account.get("logins") or [])]
    current = next((account for account in local if account.get("active")), None)

    def over_line(account: dict[str, Any], meters: tuple[str, ...] = NOTIFY_METERS) -> dict[str, Any] | None:
        hot = [limit for limit in account.get("limits", [])
               if limit.get("meter_id") in meters and limit.get("used_percent") is not None
               and float(limit["used_percent"]) >= threshold]
        return max(hot, key=lambda limit: float(limit["used_percent"]), default=None)

    def answer(account: dict[str, Any] | None, stay: bool, reason: str) -> dict[str, Any]:
        return {"email": account.get("email") if account else None,
                "display": account.get("display") if account else None,
                "account": account.get("account") if account else None,
                "stay": stay, "reason": reason}

    hot = over_line(current) if current else None
    if current and not hot:
        return answer(current, True, f"{current['display']} is under {threshold:.0f}% on every meter")

    candidates = [account for account in local
                  if account is not current and not over_line(account, ("session",))]
    alternative = best_alternative(candidates, now, exclude=current and current.get("display"),
                                   threshold=threshold)
    if alternative:
        target = next(account for account in candidates if account.get("display") == alternative["display"])
        why = (f"{current['display']} {PICK_METER_LABELS.get(hot.get('meter_id'), 'meter')} at "
               f"{float(hot['used_percent']):.0f}%; " if current and hot else "")
        room = (f"{alternative['display']} has the most room "
                f"({PICK_METER_LABELS.get(alternative['meter_id'], 'weekly')} "
                f"{alternative['used_percent']:.0f}% vs ◆ {alternative['pace_percent']:.0f}%)")
        return answer(target, False, why + room)
    if current:
        return answer(current, True, f"no other login here has room; staying on {current['display']}")
    return answer(None, True, "no usable Claude login on this machine")


_POOL_LOOKUP = {"weekly_all": _weekly_all, "weekly_fable": _weekly_fable}
_POOL_LABEL = {"weekly_all": "Weekly · all models", "weekly_fable": "Weekly · Fable"}


def claude_pool(
    accounts: list[dict[str, Any]], now: dt.datetime, meter_id: str = "weekly_all",
) -> dict[str, Any] | None:
    """The Anthropic weeklies read as ONE budget, across their offset windows.

    `meter_id` picks which weekly gets pooled — `weekly_all` (the default, the
    hero row's own column) or `weekly_fable`, so the hero table's Fable column
    gets the identical pooled-read treatment with no second implementation.

    Three Max 20x subscriptions are three separate quotas on three separate
    clocks, and no single row can say whether the *fleet* is ahead or behind.
    This one can, because both halves of the glide-slope comparison average
    cleanly when the plans are the same size:

        used  = Σ usedᵢ / n     — share of the pool's capacity already spent
        ◆     = Σ phaseᵢ / n    — where even burn would have the pool by now

    phaseᵢ is how far account i is through its own window, which is exactly what
    even burn predicts usedᵢ to be. So the mean of the phases is the pool's own
    even-burn mark, offset windows and all, and the existing ◆ vocabulary carries
    over untouched: above the mark is ahead of budget, below is capacity going to
    waste. (It generalizes to unequal plans by weighting each term by its cap;
    all three are 20x, so the weights are 1.)

    **The number is a floor whenever any component is.** A stale account's used
    can only have gone up since it was read, and an unread window contributes at
    least zero — so the sum understates and never overstates. That asymmetry is
    worth keeping rather than hiding, because it makes one verdict free: a floor
    already above the mark is ahead of budget, certainly. A floor below the mark
    decides nothing, and says so.
    """
    lookup = _POOL_LOOKUP[meter_id]
    parts: list[dict[str, Any]] = []
    for account in accounts:
        # A dormant account has no position, so it is not merely absent from
        # the pool's average — including it (even at 0%) would understate the
        # pool by diluting real burn with a subscription that isn't running.
        if account.get("provider") != "Claude" or account.get("dormant"):
            continue
        weekly = lookup(account)
        if weekly is None or not isinstance(weekly.get("resets_at"), dt.datetime):
            continue
        pace = even_pace_percent(weekly, now)
        if pace is None:
            continue
        percent = weekly.get("used_percent")
        parts.append({
            "display": account.get("display"),
            "plan": account.get("plan"),
            "used_percent": percent,
            "pace_percent": pace,
            "resets_at": weekly["resets_at"],
            # a stale read is a floor; an unread window contributes only its zero
            "floor": bool(account.get("stale")) or percent is None,
            "unread": percent is None,
        })
    # Both halves of the comparison are unweighted means, which only say
    # something true when the caps behind them are the same size. With one
    # account on Pro and the others on Max 20x, averaging a small allowance
    # with two large ones would invent headroom the fleet does not have. So the pool is the largest group of
    # accounts that share one plan, and it says which plan it pooled.
    groups: dict[str, list[dict[str, Any]]] = {}
    for part in parts:
        groups.setdefault(str(part.get("plan") or "unknown plan"), []).append(part)
    if not groups:
        return None
    plan, parts = max(groups.items(), key=lambda item: (len(item[1]), item[0] == CLAUDE_PLAN))
    if len(parts) < 2:  # a "pool" of one is just that account's row again
        return None

    count = len(parts)
    used = sum(float(p["used_percent"] or 0.0) for p in parts) / count
    pace = sum(float(p["pace_percent"]) for p in parts) / count
    floor = any(p["floor"] for p in parts)
    nearest = min(parts, key=lambda p: p["resets_at"])
    if used > pace:
        verdict = "ahead"          # safe on a floor: the truth is only higher
    elif floor:
        verdict = "indeterminate"  # a floor below the mark decides nothing
    elif used < pace:
        verdict = "trailing"
    else:
        verdict = "even"
    return {
        "provider": "Claude",
        "display": "Pooled",
        "plan": plan,
        "label": _POOL_LABEL[meter_id],
        "meter_id": meter_id,
        "count": count,
        "accounts": [p["display"] for p in parts],
        "used_percent": round(used, 6),
        "pace_percent": round(pace, 6),
        "floor": floor,
        "unread_count": sum(1 for p in parts if p["unread"]),
        "verdict": verdict,
        "next_reset_at": nearest["resets_at"],
        "next_reset_account": nearest["display"],
        "components": parts,
    }


# What each subscription costs a month, the weight a weekly carries in the total
# pool. Keyed by (provider, plan) as the accounts report them. A subscription
# not listed here is left out of the total rather than guessed at.
PLAN_PRICE_USD = {
    ("Claude", "Max 20x"): 200.0,
    ("Claude", "Max 5x"): 100.0,
    ("Claude", "Pro"): 20.0,
    ("Codex", "20x"): 200.0,
    ("Grok", "SuperGrok"): 30.0,
}
# Kimi is deliberately not pooled (operator, 2026-09-14).
TOTAL_POOL_PROVIDERS = ("Claude", "Codex", "Grok")


def total_pool(accounts: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any] | None:
    """Every pooled weekly — Claude, Codex and Grok, not Kimi — read as ONE budget.

    The same comparison as `claude_pool`, generalized the way its docstring
    says it does: the plans are different sizes, so each term is weighted by
    what the subscription costs.

        used = Σ priceᵢ·usedᵢ / Σ priceᵢ      ◆ = Σ priceᵢ·phaseᵢ / Σ priceᵢ

    It is a floor whenever any component is, for the same reason.
    """
    parts: list[dict[str, Any]] = []
    for account in accounts:
        provider = account.get("provider")
        if provider not in TOTAL_POOL_PROVIDERS or account.get("dormant"):
            continue
        price = PLAN_PRICE_USD.get((provider, account.get("plan")))
        weekly = _weekly_all(account)
        if price is None or weekly is None or not isinstance(weekly.get("resets_at"), dt.datetime):
            continue
        pace = even_pace_percent(weekly, now)
        if pace is None:
            continue
        percent = weekly.get("used_percent")
        parts.append({
            "provider": provider,
            "display": account.get("display"),
            "plan": account.get("plan"),
            "price_usd": price,
            "used_percent": percent,
            "pace_percent": pace,
            "resets_at": weekly["resets_at"],
            "floor": bool(account.get("stale")) or percent is None,
            "unread": percent is None,
        })
    if len({p["provider"] for p in parts}) < 2:  # one provider is its own pool already
        return None
    weight = sum(p["price_usd"] for p in parts)
    used = sum(p["price_usd"] * float(p["used_percent"] or 0.0) for p in parts) / weight
    pace = sum(p["price_usd"] * float(p["pace_percent"]) for p in parts) / weight
    floor = any(p["floor"] for p in parts)
    nearest = min(parts, key=lambda p: p["resets_at"])
    if used > pace:
        verdict = "ahead"
    elif floor:
        verdict = "indeterminate"
    elif used < pace:
        verdict = "trailing"
    else:
        verdict = "even"
    return {
        "provider": "Total",
        "display": "Total pool",
        "label": "Weekly · total pool",
        "meter_id": "total_pool",
        "count": len(parts),
        "accounts": [p["display"] for p in parts],
        "monthly_usd": weight,
        "used_percent": round(used, 6),
        "pace_percent": round(pace, 6),
        "floor": floor,
        "unread_count": sum(1 for p in parts if p["unread"]),
        "verdict": verdict,
        "next_reset_at": nearest["resets_at"],
        "next_reset_account": nearest["display"],
        "components": parts,
    }


def active_claude_display(accounts: list[dict[str, Any]]) -> str | None:
    """The call-sign of the Claude account currently logged into Claude Code.

    Trusts the `active` flag, which reconcile_login() has already corrected from
    the live config — so this names the live account even if its gauge is stale.
    """
    return next((a["display"] for a in accounts
                 if a.get("provider") == "Claude" and a.get("active")), None)


def satellite_logins(accounts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(satellite, call-sign) for every Claude login in the fleet, local first.

    Ordered by where the reader stands: this machine's login is the one that
    governs the next thing they type, so it leads; the rest are context.
    """
    seen: list[tuple[str, str]] = []
    for account in accounts:
        if account.get("provider") != "Claude":
            continue
        for satellite in account.get("logins") or []:
            seen.append((satellite, str(account.get("display"))))
    seen.sort(key=lambda pair: (pair[0] != LOCAL_SATELLITE, pair[0]))
    return seen


def render_login_banner(accounts: list[dict[str, Any]], *, color: bool = False) -> str:
    """Who is logged in, on every satellite that holds a login.

    Two marks, deliberately unequal: ● for this machine, ◦ for the rest. The
    local login is the one that decides what the next prompt costs; a remote one
    is a fact about the pool, and must not compete for the same attention.
    """
    logins = satellite_logins(accounts)
    if not logins:
        live = active_claude_display(accounts)
        text = f"Logged in: Claude · {live}" if live else "Logged in: (no active Claude account)"
        return (f"{paint('●', 'green', 'bold', on=True)} {paint(text, 'bold', on=True)}"
                if color else f"**● {text}**")
    # A satellite can hold several logins; only the selected one decides what the
    # next session here costs, so it alone leads. The rest read like context.
    local_displays = [display for satellite, display in logins if satellite == LOCAL_SATELLITE]
    selected = active_claude_display(accounts)
    lead = selected if selected in local_displays else next(iter(local_displays), None)
    logins.sort(key=lambda pair: (pair[0] != LOCAL_SATELLITE, pair[1] != lead, pair[0]))
    parts = []
    for satellite, display in logins:
        local = satellite == LOCAL_SATELLITE and display == lead
        body = f"Logged in · {satellite}: Claude · {display}" if local else f"{satellite}: Claude · {display}"
        if color:
            mark = paint("●", "green", "bold", on=True) if local else paint("◦", "dim", on=True)
            parts.append(f"{mark} {paint(body, 'bold', on=True) if local else paint(body, 'dim', on=True)}")
        else:
            parts.append(f"**● {body}**" if local else f"◦ {body}")
    return "   ".join(parts)


POOL_VERDICTS = {"ahead": "ahead", "trailing": "banking", "even": "even", "indeterminate": "undecided"}
POOL_TONES = {"ahead": "yellow", "trailing": "cyan", "even": "green", "indeterminate": "dim"}


def _pool_used_cell(pool: dict[str, Any], *, color: bool) -> str:
    """The used/pace/verdict grammar a pooled weekly renders, shared by both
    the all-models pool and the Fable pool — one cell shape, two meters.
    """
    used = format_percent(pool["used_percent"]) + (" floor" if pool["floor"] else "")
    cell = format_used(used, pool["pace_percent"], color=color, level=pool["used_percent"])
    delta = pool["used_percent"] - pool["pace_percent"]
    sign = "+" if delta >= 0 else "−"
    verdict = POOL_VERDICTS[pool["verdict"]]
    tail = verdict if pool["verdict"] == "indeterminate" else f"{sign}{abs(delta):.1f} {verdict}"
    return cell + " · " + paint(tail, POOL_TONES[pool["verdict"]], on=color)


def _pool_row(
    pool: dict[str, Any], now: dt.datetime, *, color: bool, fable_pool: dict[str, Any] | None = None,
) -> tuple[str, str, str, str]:
    """The pooled Anthropic weeklies, in the same four columns as the hero row.

    It reads as a sum because it is one, and it never pretends to be a reading:
    `floor` says the components under it are not all fresh, so the true position
    is this or higher. The reset column names the *next* account to come back,
    because that is the only reset a pooled budget actually has. `fable_pool` is
    the Fable pool for the same accounts (`claude_pool(..., meter_id="weekly_fable")`)
    — `None` when there aren't ≥2 Claude accounts with a readable Fable window,
    same as any other pool of fewer than two.
    """
    name = f"Claude · pooled ({pool['count']}"
    name += f" · {pool['plan']})" if pool.get("plan") else ")"
    cell = _pool_used_cell(pool, color=color)
    fable_cell = _pool_used_cell(fable_pool, color=color) if fable_pool is not None else "—"
    left = format_countdown(pool["next_reset_at"] - now) if pool["next_reset_at"] > now else "now"
    when = pool["next_reset_at"].astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p %Z")
    return (name, cell, fable_cell, f"next {pool['next_reset_account']} in {left} – {when}")


def render_weekly_summary(
    accounts: list[dict[str, Any]], openrouter: dict[str, Any] | None, now: dt.datetime, *, color: bool = False
) -> str:
    """One row per provider, weekly-only — the at-a-glance 'how's the week going'.

    Fable is a first-class column here, not a footnote: it is usually the
    binding weekly (PROVIDERS.md), so a hero read that only showed all-models
    could sit at a calm 20% while Fable was already capped. Providers without a
    Fable meter render `—`, never a blank — the column count never changes.
    """
    rows: list[tuple[str, str, str, str]] = []
    last_claude = -1
    for account in accounts:
        claude = account["provider"] == "Claude"
        if claude and account.get("dormant"):
            # One row, not zero: the roster stays legible even for a lapsed
            # subscription. Dressed down like an elsewhere-held account, and
            # every numeric column reads `—` — a dormant account has no
            # position, so nothing here may look like one.
            name = f"Claude · {account['display']} · dormant"
            name = _elsewhere_name(name, color=color)
            rows.append((name, "—", "—", "—"))
            last_claude = len(rows) - 1
            continue
        weekly = _weekly_all(account)
        if not weekly:
            continue
        name = f"Claude · {account['display']}" if claude else f"{account['display']}"
        # Unequal marks for unequal facts: ● is the login governing this
        # machine, ◦ a login held elsewhere in the fleet. Any account that
        # knows its holders wears them — a shared meter (Codex) wears both.
        for satellite in account.get("logins") or (
                [LOCAL_SATELLITE] if claude and account.get("active") else []):
            name += f" ● {satellite}" if satellite == LOCAL_SATELLITE else f" ◦ {satellite}"
        if held_elsewhere(account):
            name = _elsewhere_name(name, color=color)
        fable = _weekly_fable(account)
        # A Claude account with no Fable meter is not "unmetered", it is an
        # account whose plan does not include Fable (Pro, from 2026-09-12). An
        # em dash read as "nothing to report"; `none` reports the thing.
        fable_cell = (f"{meter_badge(account, fable)} {used_cell(account, fable, now, color=color)}" if fable
                      else paint("none", "dim", on=color) if claude else "—")
        rows.append((
            name,
            f"{meter_badge(account, weekly)} {used_cell(account, weekly, now, color=color)}",
            fable_cell,
            format_reset(weekly, now),
        ))
        if claude:
            last_claude = len(rows) - 1
    pool = claude_pool(accounts, now)
    if pool is not None and last_claude >= 0:
        fable_pool = claude_pool(accounts, now, meter_id="weekly_fable")
        rows.insert(last_claude + 1, _pool_row(pool, now, color=color, fable_pool=fable_pool))
    everything = total_pool(accounts, now)
    if everything is not None:
        left = (format_countdown(everything["next_reset_at"] - now)
                if everything["next_reset_at"] > now else "now")
        when = everything["next_reset_at"].astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p %Z")
        rows.append((f"Total · pooled ({everything['count']} · ${everything['monthly_usd']:.0f}/mo, not Kimi)",
                     _pool_used_cell(everything, color=color), "—",
                     f"next {everything['next_reset_account']} in {left} – {when}"))
    if openrouter:
        remaining = openrouter.get("remaining_usd")
        cap = openrouter.get("limit_usd")
        reset = openrouter.get("limit_reset")
        if remaining is not None and cap is not None:
            tail = f"rolling 7d · ${remaining:.0f} left of ${cap:.0f}/{reset or 'mo'}"
        else:
            tail = f"rolling 7d · resets {reset}" if reset else "rolling 7d"
        rows.append(("OpenRouter", f"${openrouter['weekly_usd']:.2f}", "—", tail))
    if not rows:
        return ""
    data_rows = [
        [name, used, fable, paint(reset, "dim", on=color)]
        for name, used, fable, reset in rows
    ]
    return "\n".join([""] + banner_table("Weekly status", ["Account", "Weekly", "Fable", "Next reset at"],
                                         data_rows, color=color))


def render_harness_spend(summary: dict[str, Any] | None, now: dt.datetime, *, color: bool = False) -> str:
    """Compact per-harness OpenRouter attribution, below the cross-provider table.

    Attribution only — no reconciliation against the /key gauge. Observed
    attributed spend does not cleanly match the gauge (the harnesses appear to
    bill a different key/account than glideslope.py queries), so a residual would
    mislead. The authoritative gauge already appears in the Weekly-status table.
    """
    if not summary:
        return ""
    breakdown = " · ".join(f"{h} ${c:.2f}" for h, c in summary["harness_today"] if c) or "—"
    title = paint("OpenRouter", "bold", on=color) if color else "**OpenRouter**"
    lines = [
        "",
        f"{title} (harness spend attributed, metered)",
        "",
        f"- today  ${summary['today']:.2f}   {breakdown}",
        f"- 7d     ${summary['week']:.2f}",
        f"- 30d    ${summary['month']:.2f}",
        _render_import_age(summary.get("latest_ts"), now, color=color),
    ]
    lines.extend(_render_subscription_table(summary.get("subscription") or [], color=color))
    return "\n".join(lines)


def _ledger_usd(value: Any) -> str:
    value = float(value or 0)
    if value >= 10_000:
        return f"${value:,.0f}"
    return f"${value:,.2f}" if value < 100 else f"${value:,.0f}"


def _spend_summary_lines(data: dict[str, Any]) -> list[str]:
    """The ledger's totals, per-account rows and leverage, as the CLI prints them."""
    t = data["totals"]
    lines = [
        f"  24h {_ledger_usd(t['d1']['usd'])} · 7d {_ledger_usd(t['d7']['usd'])} · 30d {_ledger_usd(t['d30']['usd'])} · "
        f"all {_ledger_usd(t['all']['usd'])} ({float(t['all'].get('tokens', 0)) / 1e9:.1f}B tokens since "
        f"{str(data.get('first_request_at') or '—')[:10]})",
        "",
        f"  {'account':<13}{'window':>10}{'Fable':>10}{'$/1% wk':>9}{'7d':>10}{'30d':>10}{'all':>10}",
    ]
    for a in data.get("accounts") or []:
        w = a.get("window") or {}
        per = w.get("usd_per_pct_all")
        lines.append(
            f"  {str(a.get('name', '—')):<13}{(_ledger_usd(w['usd']) if w else '—'):>10}"
            f"{(_ledger_usd(w.get('fable_usd')) if w else '—'):>10}{(_ledger_usd(per) if per else '—'):>9}"
            f"{_ledger_usd(a['d7']['usd']):>10}{_ledger_usd(a['d30']['usd']):>10}{_ledger_usd(a['all']['usd']):>10}")
    lv = data.get("leverage") or {}
    if lv.get("claude_multiple"):
        lines += ["", f"  Claude, last 30 days: {_ledger_usd(lv.get('claude_api_equivalent_d30_usd'))} API-equivalent on "
                      f"${float(lv.get('claude_subscriptions_monthly_usd') or 0):.0f}/mo of plans = {lv['claude_multiple']}×"]
    return lines


def render_api_equivalent(now: dt.datetime, *, color: bool = False) -> str:
    """What the tokens behind the meters would cost at API list prices.

    A valuation, not a bill: the subscriptions are what was paid. Read from the
    optional `<store>/spend.json` ledger (its producer is not part of this
    program); absent or malformed, the section is simply absent.
    """
    try:
        data = json.loads((STORE_DIR / "spend.json").read_text())
        if not isinstance(data, dict) or not data.get("totals"):
            return ""
        body = _spend_summary_lines(data)
    except Exception:  # noqa: BLE001 — a broken ledger must never take the report down
        return ""
    title = paint("API-equivalent", "bold", on=color) if color else "**API-equivalent**"
    generated = parse_timestamp(data.get("generated_at"))
    age = f"priced {format_countdown(now - generated)} ago" if generated else "age unknown"
    return "\n".join(["", f"{title} (list prices; not money spent · {age})", *body])


def _render_import_age(latest: dt.datetime | None, now: dt.datetime, *, color: bool = False) -> str:
    """Say when the collector last imported anything.

    The collector logs only on import, so a quiet log means either 'nothing was spent'
    or 'nothing was collected' — indistinguishable without this line, and the second
    reading renders as a calm $0.00. Past STALE_IMPORT_HOURS it is called out.
    """
    if latest is None:
        return f"- last import  {paint('never', 'red', on=color)}"
    age = now - latest
    stale = age > dt.timedelta(hours=STALE_IMPORT_HOURS)
    text = f"{format_countdown(age)} ago"
    if stale:
        text = paint(f"{text} ⚠ zeroes above may mean 'not collected'", "red", on=color)
    return f"- last import  {text}"


def _render_subscription_table(rows: list[dict[str, Any]], *, color: bool = False) -> list[str]:
    """Flat-rate harnesses: no marginal cost, so tokens and cache efficiency are the signal.

    The ≈ API column is a shadow estimate at list price — what this work would have cost
    billed per-token. It is never a position and never reconciled against a gauge.
    """
    if not rows:
        return []
    shown = [r for r in rows
             if r["cache_read"] + r["fresh"] + r["out"] >= SUBSCRIPTION_ROW_FLOOR]
    dropped = len(rows) - len(shown)
    if not shown:
        return []
    table: list[list[str]] = []
    for row in shown:
        cache_read, fresh, out = row["cache_read"], row["fresh"], row["out"]
        prompt = cache_read + fresh
        hit = f"{100.0 * cache_read / prompt:.1f}%" if prompt else "—"
        shadow = shadow_cost(row["model"], fresh, out, cache_read)
        table.append([
            row["harness"],
            row["model"] or "—",
            f"{row['calls']:,}",
            f"{cache_read / 1e6:,.1f}M",
            f"{fresh / 1e6:,.2f}M",
            hit,
            f"${shadow:,.2f}" if shadow is not None else "—",
        ])
    lines = ["", *banner_table(
        "Subscription harnesses · 30d (metered, no marginal cost)",
        ["Harness", "Model", "Calls", "Cached", "Fresh", "Hit", "≈ API list"],
        table, color=color,
    )]
    if dropped:
        floor_m = SUBSCRIPTION_ROW_FLOOR / 1e6
        lines.append(paint(f"  +{dropped} row(s) under {floor_m:.0f}M tokens not shown", "dim", on=color))
    return lines


def render_codex_resets(accounts: list[dict[str, Any]], now: dt.datetime, *, color: bool = False) -> str:
    """Codex reset banks: one row per banked full-reset credit, soonest-expiring first.

    Each credit instantly clears the weekly meter, so however hot the Codex weekly
    row reads, that cap is effectively higher on demand — but the credits expire
    (~30 days from grant), so the expiry and time-left are the point of the table.
    """
    codex = next((account for account in accounts if account.get("provider") == "Codex"), None)
    banked = codex.get("banked_resets") if codex else None
    credits = (banked or {}).get("credits") or []
    if not credits:
        return ""

    def when(value: Any) -> str:
        return value.astimezone(LOCAL_TZ).strftime("%a %b %-d") if isinstance(value, dt.datetime) else "—"

    rows: list[list[str]] = []
    for index, credit in enumerate(credits, start=1):
        expires = credit.get("expires_at")
        if isinstance(expires, dt.datetime):
            days = (expires - now).total_seconds() / 86400
            left = f"{days:.1f}d" if days >= 0 else "expired"
        else:
            left = "—"
        rows.append([str(index), credit.get("title") or "Full reset", when(credit.get("granted_at")),
                     when(expires), left])
    headers = ["#", "Credit", "Granted", "Expires", "Time left"]
    return "\n".join([""] + banner_table("Codex reset banks", headers, rows, color=color))


def _login_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_login_state(path: Path, email: str, alias: str, observed_at: dt.datetime) -> None:
    """Atomically checkpoint the account Glideslope most recently observed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            json.dump({
                "schema": 1,
                "email": email,
                "alias": alias,
                "observed_at": iso_utc(observed_at),
            }, handle, separators=(",", ":"))
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


def _latest_login_command(path: Path, *, after: dt.datetime | None) -> dt.datetime | None:
    """Return the latest Claude Code `/login` command after ``after``.

    Claude's global history records slash commands even though they do not enter
    project transcripts. The timestamp is epoch milliseconds. A command alone
    is not proof of a switch; callers must also observe that the live email
    changed before turning it into an event.
    """
    latest: dt.datetime | None = None
    try:
        with path.open(errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or str(record.get("display") or "").strip() != "/login":
                    continue
                raw = record.get("timestamp")
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                try:
                    when = dt.datetime.fromtimestamp(float(raw) / 1000, dt.timezone.utc)
                except (OSError, OverflowError, ValueError):
                    continue
                if after is not None and when <= after:
                    continue
                if latest is None or when > latest:
                    latest = when
    except (FileNotFoundError, OSError):
        return None
    return latest


def _append_observed_switch(
    path: Path, *, when: dt.datetime, from_alias: str, to_alias: str, source: str
) -> dict[str, Any]:
    """Append a secret-free switch inferred from a change of login.

    Every switch is one of these now: accounts change by web login, which this
    program can only observe after the fact, so the outgoing account's numbers at
    the moment of departure are not recoverable and the record says so.
    """
    record = {
        "at": iso_utc(when),
        "from": from_alias,
        "to": to_alias,
        "source": source,
        "note": "usage unavailable; switch was observed after authentication",
        "usage": {from_alias: None, to_alias: None},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return record


def _matching_switch_exists(
    switches: list[dict[str, Any]], from_alias: str, to_alias: str, after: dt.datetime | None
) -> bool:
    # Allow a small clock/order margin: claude-account journals immediately
    # before it swaps ~/.claude.json, while Glideslope observes immediately after.
    floor = after - dt.timedelta(minutes=5) if after is not None else None
    for record in switches:
        if record.get("from") != from_alias or record.get("to") != to_alias:
            continue
        when = parse_timestamp(record.get("at"))
        if when is not None and (floor is None or when >= floor):
            return True
    return False


def observe_login_switch(
    accounts: list[dict[str, Any]], live_email: str, now: dt.datetime, *,
    state_path: Path = LOGIN_OBSERVER_STATE, history_path: Path = CLAUDE_HISTORY,
    switch_path: Path = SWITCH_LOG, lock_path: Path = LOGIN_OBSERVER_LOCK,
) -> dict[str, Any] | None:
    """Journal account changes made through Claude Code's built-in `/login`.

    A built-in `/login` changes account identity without updating the usage
    journal, so this observer is the switch history path. A `/login` leaves two
    independent facts — its exact command time in history.jsonl and the resulting
    email in ~/.claude.json — and persisting the last observed email lets a later
    Glideslope run join them into one event.

    The first run can also repair a missed switch when the journal's last
    destination disagrees with the live account and a later `/login` exists.
    Synthetic events intentionally leave usage blank; a gauge fetched later is
    not an honest snapshot of the instant the switch occurred.
    """
    current = next((account for account in accounts
                    if account.get("provider") == "Claude" and account.get("email") == live_email), None)
    if current is None or not current.get("account"):
        return None
    current_alias = str(current["account"])

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # another Glideslope process will record and checkpoint it
        os.chmod(lock_path, 0o600)

        state = _login_state(state_path)
        prior_email = state.get("email") if isinstance(state.get("email"), str) else None
        prior_alias = state.get("alias") if isinstance(state.get("alias"), str) else None
        prior_observed = parse_timestamp(state.get("observed_at"))
        switches = load_switches(switch_path)
        event: dict[str, Any] | None = None

        if prior_email and prior_email != live_email and prior_alias and prior_alias != current_alias:
            if not _matching_switch_exists(switches, prior_alias, current_alias, prior_observed):
                # Authentication can outlive a watch tick: /login may be entered,
                # Glideslope may observe the old email once more, and only then
                # does the browser flow complete. Keep that command in reach.
                history_floor = (prior_observed - dt.timedelta(minutes=15)
                                 if prior_observed is not None else None)
                login_at = _latest_login_command(history_path, after=history_floor)
                event = _append_observed_switch(
                    switch_path,
                    when=login_at or now,
                    from_alias=prior_alias,
                    to_alias=current_alias,
                    source="claude-code-login" if login_at else "observed",
                )
        elif not prior_email and switches:
            # Bootstrap repair: the existing ledger tells us who was active last.
            # A later /login plus a different live account closes the missing edge.
            latest = switches[0]
            ledger_alias = latest.get("to")
            ledger_at = parse_timestamp(latest.get("at"))
            if isinstance(ledger_alias, str) and ledger_alias != current_alias:
                login_at = _latest_login_command(history_path, after=ledger_at)
                if login_at is not None:
                    event = _append_observed_switch(
                        switch_path,
                        when=login_at,
                        from_alias=ledger_alias,
                        to_alias=current_alias,
                        source="claude-code-login",
                    )

        _write_login_state(state_path, live_email, current_alias, now)
        return event


def load_switches(path: Path = SWITCH_LOG, *, limit: int = SWITCH_HISTORY_LIMIT) -> list[dict[str, Any]]:
    """Read the most recent switch events (newest first). Tolerant of a partial log."""
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, OSError):
        return []
    switches: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("at"):
            switches.append(record)
    switches.sort(key=lambda item: str(item.get("at")), reverse=True)
    return switches[:limit]


def _switch_usage_cell(usage: dict[str, Any] | None) -> str:
    if not isinstance(usage, dict):
        return "—"
    parts = []
    for label, short in SWITCH_BUCKETS:
        value = usage.get(label)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{short} {format_percent(float(value))}")
    return " · ".join(parts) if parts else "—"


def _switch_when(record: dict[str, Any]) -> str:
    when = parse_timestamp(record.get("at"))
    return when.astimezone(LOCAL_TZ).strftime("%b %-d, %-I:%M %p") if when else str(record.get("at"))


def render_switches(switches: list[dict[str, Any]], *, color: bool = False) -> str:
    if not switches:
        return ""
    headers = ["When", "Switch", "Left (5h · wk · Fable)", "Entered (5h · wk · Fable)"]
    rows: list[list[str]] = []
    for record in switches:
        frm, to = str(record.get("from")), str(record.get("to"))
        usage = record.get("usage") or {}
        source = record.get("source")
        route = f"{call_sign(frm)} → {call_sign(to)}"
        if source == "claude-code-login":
            route += " · /login"
        elif source == "observed":
            route += " · observed"
        left = f"{call_sign(frm)}: {_switch_usage_cell(usage.get(frm))}"
        entered = f"{call_sign(to)}: {_switch_usage_cell(usage.get(to))}"
        rows.append([_switch_when(record), route, left, entered])
    return "\n".join([""] + banner_table(f"Recent switches (last {len(switches)})", headers, rows, color=color))


def json_convert(value: Any) -> Any:
    """Recursively render datetimes as ISO-8601 so a payload is always dumpable.

    Every branch of the JSON payload must pass through here, not just `accounts`:
    harness_spend carries a `latest_ts` datetime, and skipping it turned an
    ordinary spend-attribution read into a hard TypeError that took the whole
    --json surface — and with it every view build — down.
    """
    if isinstance(value, dt.datetime):
        return iso_utc(value)
    if isinstance(value, list):
        return [json_convert(item) for item in value]
    if isinstance(value, dict):
        return {key: json_convert(item) for key, item in value.items()}
    return value


def json_ready(accounts: list[dict[str, Any]], generated_at: dt.datetime, warnings: list[str]) -> dict[str, Any]:
    return {"generated_at": iso_utc(generated_at), "accounts": json_convert(accounts),
            "warnings": warnings}


def call_with_deadline(query: Any, seconds: float, name: str) -> Any:
    """Run a provider query with a hard wall-clock deadline, or raise PositionError.

    A query's own timeouts only start once it is talking to something. On
    2026-09-13 the Codex read wedged BEFORE that — `codex app-server` sat in
    uninterruptible state while spawning, Popen blocked on it, and every sampler
    run died at its 180s kill, taking Claude's fresh read down with it. The
    query runs on a daemon thread instead; a wedged one is abandoned (it cannot
    outlive the process) and the provider degrades to its last good read.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = query()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, name=f"glideslope-{name}", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise PositionError(f"{name} read exceeded {seconds:.0f}s; showing its last good read")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def read_provider(
    name: str,
    query: Any,
    normalize: Any,
    adapt: Any,
    *,
    saved: Path | None,
    now: dt.datetime,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Read one provider live, falling back to its last good read when that fails.

    A transient transport failure — a slow Codex app-server, a dropped Kimi
    request — used to delete the provider from the position outright, which the
    surfaces cannot distinguish from "this subscription does not exist". The
    numbers are cumulative and the reset clocks absolute, so the honest
    degradation is the last observation, carrying its own age and marked stale.
    """
    if saved is not None:  # an explicit saved response (tests/debugging) is never cached
        try:
            return adapt(normalize(read_snapshot(saved), now))
        except PositionError as exc:
            warnings.append(str(exc))
            return []

    try:
        observed_at = utc_now()
        raw = call_with_deadline(query, PROVIDER_DEADLINE_SECONDS, name)
        accounts = adapt(normalize(raw, observed_at))
        cache_provider_read(name, raw, observed_at)
        return accounts
    except PositionError as exc:
        fallback = cached_provider_read(name, now)
        if fallback is None:
            warnings.append(str(exc))
            return []
        raw, observed_at = fallback
        try:
            accounts = adapt(normalize(raw, observed_at))
        except PositionError:
            warnings.append(str(exc))
            return []
        for account in accounts:
            account["stale"] = True
        warnings.append(f"{exc}; showing the last known read from {iso_utc(observed_at)}")
        return accounts


def gather(args: argparse.Namespace) -> tuple[dt.datetime, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Fetch the full position once: Claude + Codex + Kimi + Grok + OpenRouter + harness spend + switches.

    The single fetch path, shared by the one-shot report and the --watch loop. The
    watch loop calls this only on its slow refetch tick; between ticks it re-renders
    the returned data against a fresh `now`, so the ◆ pace and reset countdown stay
    live off local math without touching any API.
    """
    now = utc_now()
    accounts: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Other satellites first: each publishes the one account it can read live, so
    # the fleet sees more of the position than this machine alone ever could.
    satellites = [] if args.skip_satellites else read_satellites(now, warnings)

    # Who is logged in here, read before the gauge so a login change expires it.
    local_emails = local_login_emails()
    live_email = selected_login_email(logins=local_emails)

    if not args.skip_claude:
        try:
            claude, observed_at, warning = load_claude_snapshot(
                args.claude_snapshot,
                refresh=not args.no_refresh_claude,
                max_age_seconds=max(0, args.max_age_seconds),
                now=now,
                # Only the real cache describes this machine's logins; a snapshot
                # handed in by a test or a debugger never triggers a live re-read.
                logins=((local_emails, live_email)
                        if args.claude_snapshot == DEFAULT_CLAUDE_SNAPSHOT else None),
            )
            merge_satellite_claude(claude, satellites, now, warnings=warnings)
            accounts.extend(keep_claude_state(
                normalize_claude(claude, observed_at), claude, now))
            if warning:
                warnings.append(warning)
        except PositionError as exc:
            warnings.append(str(exc))

    # Correct who's-active from the live login — instant after a switch, no API.
    # Also journal built-in /login switches, which bypass claude-account's hook.
    reconcile_login(accounts, live_email)
    reconcile_satellite_logins(accounts, local_emails, satellites, live_email)
    if accounts and live_email:
        try:
            observe_login_switch(accounts, live_email, now)
        except (OSError, ValueError) as exc:
            warnings.append(f"Claude login observer failed: {exc}")

    # Dormancy after logins are known: a live login always outranks the
    # roster's word, so `logins` must already be populated before this join.
    claude_roster = read_roster(args.claude_roster)
    reconcile_plans(accounts, claude_roster)
    reconcile_dormant(accounts, claude_roster, warnings)

    if not args.skip_codex:
        accounts.extend(read_provider(
            "codex", query_codex_rate_limits, normalize_codex, codex_accounts,
            saved=args.codex_snapshot, now=now, warnings=warnings,
        ))
        reconcile_shared_logins(accounts, "Codex", codex_login_email(), satellites, warnings)

    if not args.skip_kimi:
        accounts.extend(read_provider(
            "kimi", query_kimi, normalize_kimi, kimi_accounts,
            saved=args.kimi_snapshot, now=now, warnings=warnings,
        ))

    if not args.skip_grok:
        accounts.extend(read_provider(
            "grok", query_grok, normalize_grok, grok_accounts,
            saved=args.grok_snapshot, now=now, warnings=warnings,
        ))
        reconcile_shared_logins(accounts, "Grok", grok_login_email(), satellites, warnings)

    openrouter: dict[str, Any] | None = None
    if not args.skip_openrouter:
        try:
            openrouter = normalize_openrouter(query_openrouter(), utc_now())
        except PositionError as exc:
            warnings.append(str(exc))

    # Local attribution under the OpenRouter gauge. Never allowed to break the
    # Claude/Codex path, so any failure just yields None.
    harness_spend = None
    if not args.skip_harness_spend:
        try:
            harness_spend = query_harness_spend(now)
        except Exception:  # noqa: BLE001 — supplementary data must never raise
            harness_spend = None

    # One seam for every surface: the CLI, --json, the Detail view and the
    # popup all read the position after this, so a rolled window is advanced
    # exactly once, in one place, with one set of rules.
    roll_forward_windows(accounts, now)

    switches = [] if args.no_switches else load_switches()
    return now, accounts, openrouter, harness_spend, switches, warnings, satellites


def render_report(
    accounts: list[dict[str, Any]], openrouter: dict[str, Any] | None,
    harness_spend: dict[str, Any] | None, switches: list[dict[str, Any]],
    now: dt.datetime, warnings: list[str], *, color: bool,
) -> str:
    """The full human-facing report: banner + weekly + cross-provider table + spend + resets + switches."""
    output = render_login_banner(accounts, color=color)
    weekly = render_weekly_summary(accounts, openrouter, now, color=color)
    if weekly:
        output += "\n" + weekly
    output += "\n\n" + render_markdown(accounts, now, warnings, color=color)
    harness = render_harness_spend(harness_spend, now, color=color)
    if harness:
        output += "\n" + harness
    api_equivalent = render_api_equivalent(now, color=color)
    if api_equivalent:
        output += "\n" + api_equivalent
    resets = render_codex_resets(accounts, now, color=color)
    if resets:
        output += "\n" + resets
    if switches:
        output += "\n" + render_switches(switches, color=color)
    return output


def _watch_footer(updated: dt.datetime | None, refetch_in: int, *, color: bool) -> str:
    stamp = updated.astimezone(LOCAL_TZ).strftime("%-I:%M:%S %p") if updated else "—"
    text = f"live · updated {stamp} · refetch in {refetch_in // 60}:{refetch_in % 60:02d} · Ctrl-C to exit"
    return paint(text, "dim", on=color)


def _draw(out: Any, frame: str) -> None:
    """Repaint in place: home the cursor, clear each line to EOL, wipe any tail below."""
    out.write("\033[H")
    for line in frame.split("\n"):
        out.write(line + "\033[K\n")
    out.write("\033[J")
    out.flush()


def run_watch(args: argparse.Namespace) -> int:
    """Live display for a dedicated pane/workspace. Fetch slow, redraw fast."""
    color = args.color != "never"  # a live display is colored unless explicitly refused
    interval = max(1.0, args.interval)
    refetch = max(30, args.refetch_seconds)
    out = sys.stdout
    data: tuple[Any, ...] | None = None
    last_fetch_mono = 0.0
    last_fetch_wall: dt.datetime | None = None
    # A closed pane / `kill` sends SIGTERM, which skips `finally`; route it through
    # SystemExit so the cursor is always restored no matter how the watch ends.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    out.write("\033[?25l\033[2J")  # hide cursor, clear screen
    try:
        while True:
            mono = time.monotonic()
            if data is None or (mono - last_fetch_mono) >= refetch:
                data = gather(args)
                last_fetch_mono = mono
                last_fetch_wall = data[0]
            _, accounts, openrouter, harness_spend, switches, warnings, _sats = data
            body = render_report(accounts, openrouter, harness_spend, switches, utc_now(), warnings, color=color)
            refetch_in = max(0, int(refetch - (time.monotonic() - last_fetch_mono)))
            _draw(out, body + "\n\n" + _watch_footer(last_fetch_wall, refetch_in, color=color))
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[?25h\n")  # restore cursor
        out.flush()
    return 0


def resolved_config() -> dict[str, Any]:
    """Every configured value after defaults, as the program actually uses it."""
    return {
        "config_path": str(CONFIG_PATH),
        "config_present": CONFIG_PATH.exists(),
        "store": str(STORE_DIR),
        "keys_file": str(KEYS_FILE),
        "timezone": str(LOCAL_TZ),
        "satellite": LOCAL_SATELLITE,
        "satellites": list(REMOTE_SATELLITES),
        "claude": {"call_signs": dict(CLAUDE_CALL_SIGNS), "plan": CLAUDE_PLAN},
        "codex": {"plan": CODEX_PLAN},
        "kimi": {"plan": KIMI_PLAN},
        "grok": {"plan": GROK_PLAN},
        "notify": {"sink": NOTIFY_SINK, "url": NOTIFY_URL, "percent": NOTIFY_PERCENT},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report Claude, Codex, Kimi and Grok usage position.")
    parser.add_argument("--json", action="store_true", help="emit the normalized machine-readable snapshot")
    parser.add_argument("--no-refresh-claude", action="store_true", help="use the cached Claude gauge")
    parser.add_argument("--claude-snapshot", type=Path, default=DEFAULT_CLAUDE_SNAPSHOT)
    parser.add_argument("--claude-roster", type=Path, default=CLAUDE_ROSTER,
                        help="claude-account's roster (identity + dormancy); injectable for tests")
    parser.add_argument("--codex-snapshot", type=Path, help="use a saved app-server response (tests/debugging)")
    parser.add_argument("--kimi-snapshot", type=Path, help="use a saved usages response (tests/debugging)")
    parser.add_argument("--grok-snapshot", type=Path, help="use a saved billing response (tests/debugging)")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--skip-codex", action="store_true")
    parser.add_argument("--skip-kimi", action="store_true")
    parser.add_argument("--skip-grok", action="store_true")
    parser.add_argument("--skip-openrouter", action="store_true")
    parser.add_argument("--skip-satellites", action="store_true",
                        help="do not read other satellites' beacons (offline / tests)")
    parser.add_argument("--skip-harness-spend", action="store_true")
    parser.add_argument("--no-switches", action="store_true", help="omit the recent-switch history")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="ANSI color: auto (TTY only, default), always, or never")
    parser.add_argument("--watch", action="store_true",
                        help="live-updating display for a dedicated pane/workspace")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="--watch: seconds between screen redraws (default 10)")
    parser.add_argument("--refetch-seconds", type=int, default=150,
                        help="--watch: seconds between real API refreshes (default 150)")
    parser.add_argument("--max-age-seconds", type=int, default=CLAUDE_CACHE_SECONDS)
    parser.add_argument("--print-store", action="store_true",
                        help="print the store directory the config resolves to, and exit")
    parser.add_argument("--print-config", action="store_true",
                        help="print the resolved configuration as JSON, and exit")
    parser.add_argument("--pick", action="store_true",
                        help="name the Claude account new sessions here should use (claude-account use auto)")
    args = parser.parse_args(argv)

    if args.print_store:
        print(STORE_DIR)
        return 0
    if args.print_config:
        print(json.dumps(resolved_config(), indent=2))
        return 0

    if args.watch:
        return run_watch(args)

    if args.pick:
        # Only this machine's logins can be launched into, and a launch is waiting
        # on the answer — so read nothing that cannot change it.
        args.skip_codex = args.skip_kimi = args.skip_grok = args.skip_openrouter = True
        args.skip_harness_spend = args.skip_satellites = True

    now, accounts, openrouter, harness_spend, switches, warnings, satellites = gather(args)

    if args.pick:
        pick = pick_account(accounts, now)
        print(json.dumps(pick) if args.json else
              f"{pick['display'] or '—'} · {'stay' if pick['stay'] else 'switch'} · {pick['reason']}")
        return 0 if pick["email"] else 1

    if args.json:
        payload = json_ready(accounts, now, warnings)
        payload["openrouter"] = openrouter and json_ready([openrouter], now, [])["accounts"][0]
        payload["harness_spend"] = json_convert(harness_spend)
        payload["switches"] = json_convert(switches)
        payload["pool"] = json_convert(claude_pool(accounts, now))
        payload["pool_fable"] = json_convert(claude_pool(accounts, now, meter_id="weekly_fable"))
        payload["pool_total"] = json_convert(total_pool(accounts, now))
        # The fleet's own roster: who is logged in where, and how fresh each
        # satellite's word is. Surfaces render the login marks from this.
        payload["satellites"] = json_convert(
            [{"name": LOCAL_SATELLITE, "local": True, "age_seconds": 0.0}]
            + [{"name": s["name"], "local": False, "age_seconds": round(s["age_seconds"], 1),
                "observed_at": s["observed_at"]} for s in satellites])
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(accounts, openrouter, harness_spend, switches, now, warnings,
                            color=color_enabled(args.color)))
    return 0 if (accounts or openrouter) else 1


if __name__ == "__main__":
    raise SystemExit(main())
