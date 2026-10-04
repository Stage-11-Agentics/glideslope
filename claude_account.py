#!/usr/bin/env python3
"""claude-account — which Claude account new sessions use, and what each one has left.

For anyone who runs several Claude accounts and moves between them as limits burn down.

THIS TOOL MINTS NO TOKENS, AND THE ONLY CREDENTIALS IT READS ARE ONES IT NEVER WRITES.

Meter tokens (added 2026-09-28) are the one credential this tool reads from a file:
setup-tokens the operator minted (`claude setup-token`) and placed, one per account, in
~/.claude/accounts/meter-tokens/ (0600, on the one machine that probes). Each read is a
single 1-token request whose response headers carry the billed account's own 5h, weekly
and Fable meters and its organization ID. The ID, not the file name, says whose meters
they are: it is joined to the roster's `org_uuid`, which is learned from Claude Code's
own login record. A setup-token is inference-only and cannot read /usage, so without
this an account nobody is logged into anywhere is unreadable, and spend on it is
invisible until someone logs in (2026-09-26: a setup-token labelled for one account that
billed another). Nothing here refreshes, rotates or stores a meter token.

Login homes (added 2026-09-12). Claude Code keeps one login per config directory:
with CLAUDE_CONFIG_DIR unset it uses ~/.claude + ~/.claude.json + the keychain item
"Claude Code-credentials"; with CLAUDE_CONFIG_DIR=<dir> it uses <dir>/.claude.json
and its own keychain item, "Claude Code-credentials-<first 8 hex of sha256(dir)>".
So a machine can hold every account at once — one home each — and Claude Code
stays the only process that ever writes or refreshes a token, exactly as it does
with a single login. Switching is choosing which home a NEW session starts in.

    ~/.claude                      the default home (whatever account it holds)
    ~/.claude-profiles/<name>/     one extra home per account; every shared entry
                                   (settings, skills, hooks, projects/ transcripts…)
                                   is a symlink back into ~/.claude

This tool only reads a home's access token, and only while it is still valid. It
never performs an OAuth refresh, never writes the keychain, and stores no token.

    Why (retired 2026-08-06). The old design kept every account's refresh token so
    it could read or swap accounts on demand. Refresh tokens rotate and are
    single-use, so that store had to be written back on every read, which made a
    concurrent read a way to strand an account on dead paper. Login homes remove
    the need: each token family has exactly one owner, the Claude Code that lives
    in that home.

Commands
    status [--json]          usage: live for every account holding a login here
    homes [--json]           every login home on this machine and who it holds
    use <account|auto>       route new sessions to an account (auto = Glideslope's pick)
    home [account]           print the CLAUDE_CONFIG_DIR a new session should use
    exec [account] -- cmd    run cmd under an account's home (the shell `claude` uses this)
    login <account|name>     create a login home and open Claude Code in it for /login
    link                     refresh the shared-entry symlinks in every profile home
    list                     the roster (identities only — there are no credentials here)
    which                    the account new sessions go to, and where it is logged in
    save [alias]             record every logged-in account's identity in the roster
    note <email> --org <id>  record an account's identity with no login here [--alias name]
    remove <alias>           drop an account from the roster
    whose [file...] [--expect <account>] [--json]
                             which account a setup-token bills, verified by its org ID, and
                             that account's meters; no file = every meter token. Exit 0 when
                             every token is known (and is the expected account), 3 when one
                             bills another account, 4 when one bills an org the roster lacks,
                             2 when a probe fails

An <account> is a call-sign (Bravo), a roster alias (personal) or an email.
CLAUDE_ACCOUNT=<account> overrides the selection for one launch.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
UA = "claude-cli/2.1.208 (external, cli)"  # the usage endpoint is read as the CLI reads it (PROVIDERS.md)

CLAUDE_JSON = Path.home() / ".claude.json"
DEFAULT_HOME_DIR = Path.home() / ".claude"
PROFILES_DIR = Path.home() / ".claude-profiles"
STORE = DEFAULT_HOME_DIR / "accounts"
ROSTER = STORE / "roster.json"            # identities only, never a token
USAGE_CACHE = STORE / ".usage-cache"      # last-known-good usage per alias
SWITCH_LOG = STORE / ".switch-log.jsonl"  # shared with Glideslope's login observer
USAGE_BACKOFF = STORE / ".usage-backoff.json"  # no usage reads until this time, after a 429

# The usage endpoint throttles hard, and its limit behaves as one shared budget for
# every account on a machine: when it refuses one, it refuses them all. So a 429
# pauses every read here (this tool, and Glideslope through it) for Retry-After
# seconds, bounded, or BACKOFF_DEFAULT_S without one; and the reads within one run
# are spaced, never fired together.
BACKOFF_DEFAULT_S = 300
BACKOFF_MIN_S = 60
BACKOFF_MAX_S = 1800
READ_SPACING_S = 2.0
SELECTION = STORE / "selection.json"      # which account new sessions use — no token
METER_TOKENS = STORE / "meter-tokens"     # operator-minted setup-tokens, one per account, 0600
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
# Fable first: only a Fable request answers with the Fable weekly. Haiku is the fallback
# for an account whose plan has no Fable (it still answers 5h and weekly).
METER_MODELS = ("claude-fable-5-1", "claude-haiku-4-5-20251001")
# Response-header claim -> the limit label the usage read uses. `7d_oi` is the Fable
# weekly (verified against /usage on the same account, 2026-09-28: 7d 0.13 = weekly 13%,
# 7d_oi 0.02 = Fable 2%). A claim absent from the headers is unknown, never zero.
METER_CLAIMS = {"5h": "session (5h)", "7d": "weekly (all models)", "7d_oi": "weekly (Fable)"}
CLI_CANDIDATES = (Path.home() / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude"),
                  Path("/usr/local/bin/claude"))
METER_ORGS = STORE / ".meter-orgs.json"  # token fingerprint -> org it billed: a hash, never a token
METER_TIMEOUT_S = 10
METER_BUDGET_S = 25.0  # all probes in one run; a beacon's whole status call has 45s
GLIDESLOPE = Path(__file__).resolve().parent / "glideslope.py"

# Call-signs come from Glideslope's config ([claude] call_signs), keyed by email
# or by roster alias. Emails are the same on every machine; aliases are this
# machine's bookkeeping, so alias keys are resolved through the local roster.
# Nothing here is a credential. This tool also runs alone on satellites where
# glideslope.py is not installed, so the config is read directly.
CONFIG_PATH = Path(os.environ.get("GLIDESLOPE_CONFIG") or (Path.home() / ".glideslope" / "config.toml"))


def _configured_call_signs(path: Path = CONFIG_PATH) -> dict[str, str]:
    try:
        import tomllib
    except ModuleNotFoundError:
        return _call_signs_without_tomllib(path)
    try:
        with path.open("rb") as handle:
            table = tomllib.load(handle).get("claude", {}).get("call_signs", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(k): str(v) for k, v in table.items()} if isinstance(table, dict) else {}


def _call_signs_without_tomllib(path: Path) -> dict[str, str]:
    """Python before 3.11 (a satellite's /usr/bin/python3): read only `[claude] call_signs = { k = "v", ... }`."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    section = re.search(r"^\[claude\][ \t]*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    table = re.search(r"^[ \t]*call_signs[ \t]*=[ \t]*\{([^}]*)\}", section.group(1), re.M) if section else None
    if not table:
        return {}
    pairs = re.findall(r'(?:"([^"]+)"|([A-Za-z0-9_.@-]+))\s*=\s*"([^"]*)"', table.group(1))
    return {quoted or bare: value for quoted, bare, value in pairs}


def call_signs_by_email(roster: dict | None = None,
                        configured: dict[str, str] | None = None) -> dict[str, str]:
    """{email: call-sign} from the config; alias keys are resolved via the roster."""
    configured = _configured_call_signs() if configured is None else configured
    if not configured:
        return {}
    by_email: dict[str, str] = {}
    roster = roster_read() if roster is None else roster
    for key, name in configured.items():
        if "@" in key:
            by_email[key.lower()] = name
        else:
            email = (roster.get(key) or {}).get("email")
            if email:
                by_email[str(email).lower()] = name
    return by_email


CALL_SIGNS: dict[str, str] = {}  # filled by main() once the roster can be read

# Entries of ~/.claude a profile home keeps for itself instead of linking. They are
# per-install runtime state (the background daemon and its jobs, session sockets,
# locks, config backups that embed the login) — sharing them across logins would
# let one account's machinery act on another's sessions.
PROFILE_LOCAL = frozenset({
    ".claude.json", "backups", "daemon", "daemon.log", "sessions", "jobs",
    "scheduled_tasks.lock", "cache", "stats-cache.json", "telemetry",
    ".last-cleanup", ".last-update-result.json", ".git", ".gitignore", ".DS_Store",
    ".credentials.json",
})

# Keys copied from ~/.claude.json into a new home so it starts onboarded and with
# the same project trust. Never the login (oauthAccount) or account-scoped caches.
SEED_KEYS = (
    "hasCompletedOnboarding", "lastOnboardingVersion", "installMethod", "autoUpdates",
    "autoUpdatesProtectedForNative", "projects", "mcpServers", "githubRepoPaths",
    "teammateMode", "showSpinnerTree", "fileCheckpointingEnabled",
    "shiftEnterKeyBindingInstalled", "optionAsMetaKeyInstalled", "hasUsedBackslashReturn",
    "claudeInChromeDefaultEnabled", "hasCompletedClaudeInChromeOnboarding",
    "hasAcknowledgedCostThreshold", "effortCalloutDismissed", "effortCalloutV2Dismissed",
    "hasSeenAutoModeEntryWarning", "deepLinkTerminal",
)

C = {
    "dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "yellow": "\033[33m",
    "green": "\033[32m", "cyan": "\033[36m", "off": "\033[0m",
}
if not sys.stdout.isatty():
    C = dict.fromkeys(C, "")


def die(msg: str) -> "typing.NoReturn":  # noqa: F821
    print(f"{C['red']}error:{C['off']} {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"claude-account: {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------- login homes

class Home:
    """One place Claude Code keeps a login: a config dir, its config file, its keychain item."""

    def __init__(self, name: str, config_dir: Path | None):
        self.name = name
        self.config_dir = config_dir  # None is the default home: CLAUDE_CONFIG_DIR unset

    @property
    def is_default(self) -> bool:
        return self.config_dir is None

    @property
    def claude_json(self) -> Path:
        return CLAUDE_JSON if self.config_dir is None else self.config_dir / ".claude.json"

    @property
    def keychain_service(self) -> str:
        return keychain_service_for(self.config_dir)

    def config(self) -> dict:
        try:
            value = json.loads(self.claude_json.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def account(self) -> dict:
        return self.config().get("oauthAccount") or {}

    def email(self) -> str | None:
        email = self.account().get("emailAddress")
        return email if isinstance(email, str) and email else None

    def env_value(self) -> str:
        return "" if self.config_dir is None else str(self.config_dir)


def keychain_service_for(config_dir: Path | None) -> str:
    """Claude Code's keychain item name for a config dir (2.1.x: hash of the dir string)."""
    if config_dir is None:
        return KEYCHAIN_SERVICE
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE}-{digest}"


def homes() -> list[Home]:
    found = [Home("default", None)]
    try:
        entries = sorted(PROFILES_DIR.iterdir())
    except OSError:
        entries = []
    found.extend(Home(entry.name, entry) for entry in entries
                 if entry.is_dir() and not entry.name.startswith("."))
    return found


def home_for(email: str | None, all_homes: list[Home] | None = None) -> Home | None:
    """The home holding a login for EMAIL — the default home first, then profiles by name."""
    if not email:
        return None
    return next((home for home in (all_homes or homes()) if home.email() == email), None)


def call_sign_for(email: str | None) -> str | None:
    return CALL_SIGNS.get((email or "").lower())


def label_for(email: str | None, roster: dict | None = None) -> str:
    if not email:
        return "?"
    if email.lower() in CALL_SIGNS:
        return CALL_SIGNS[email.lower()]
    roster = roster_read() if roster is None else roster
    return next((alias for alias, entry in roster.items() if entry.get("email") == email), email)


def resolve_email(name: str, roster: dict | None = None) -> str | None:
    """An account named by call-sign, roster alias or email → its email."""
    wanted = name.strip().lower()
    if "@" in wanted:
        return wanted
    for email, sign in CALL_SIGNS.items():
        if sign.lower() == wanted:
            return email
    roster = roster_read() if roster is None else roster
    for alias, entry in roster.items():
        if alias.lower() == wanted and entry.get("email"):
            return entry["email"]
    return None


def link_profile(profile_dir: Path) -> list[str]:
    """Create any missing symlinks from a profile home back into ~/.claude.

    Only ever adds: a link that already exists, or a real file the profile made for
    itself, is left alone. Returns the names it linked.
    """
    linked = []
    try:
        entries = sorted(DEFAULT_HOME_DIR.iterdir())
    except OSError:
        return linked
    for entry in entries:
        if entry.name in PROFILE_LOCAL:
            continue
        target = profile_dir / entry.name
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(entry)
        linked.append(entry.name)
    return linked


def seed_profile_config(profile_dir: Path) -> bool:
    """Give a new home an onboarded .claude.json without anyone's login in it."""
    target = profile_dir / ".claude.json"
    if target.exists():
        return False
    try:
        source = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        source = {}
    seeded = {key: source[key] for key in SEED_KEYS if key in source}
    write_json_atomic(target, seeded)
    return True


def ensure_profile(name: str) -> Path:
    profile_dir = PROFILES_DIR / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(PROFILES_DIR, 0o700)
    seed_profile_config(profile_dir)
    link_profile(profile_dir)
    return profile_dir


# ---------------------------------------------------------------- keychain

def keychain_read(service: str = KEYCHAIN_SERVICE) -> dict | None:
    """A home's OAuth blob. Read-only — nothing here ever writes it."""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        return json.loads(raw).get("claudeAiOauth")
    except (json.JSONDecodeError, AttributeError):
        return None


def token_expired(oauth: dict, skew_s: int = 120) -> bool:
    exp = oauth.get("expiresAt")
    return not isinstance(exp, int) or exp / 1000 <= time.time() + skew_s


def token_state(home: Home) -> tuple[str, float | None]:
    """('valid', hours left) | ('expired', None) | ('none', None). Never refreshes."""
    oauth = keychain_read(home.keychain_service)
    if not oauth:
        return "none", None
    if token_expired(oauth):
        return "expired", None
    return "valid", (oauth["expiresAt"] / 1000 - time.time()) / 3600


def live_token(home: Home) -> str:
    """A home's access token, if it is usable. Never refreshes it.

    Claude Code refreshes on its own schedule while a session in that home runs, so
    a valid token is the normal case for a home in use. When it has expired we say
    so and stop: minting a new one would rotate the refresh token under Claude Code
    and is exactly the failure this tool was rewritten to remove.
    """
    oauth = keychain_read(home.keychain_service)
    if not oauth:
        raise RuntimeError(f"no credentials in the keychain for the {home.name} home")
    if token_expired(oauth):
        raise RuntimeError(f"the {home.name} home's access token has expired; Claude Code "
                           "renews it on next use there — no token is minted here")
    return oauth["accessToken"]


# ---------------------------------------------------------------- roster
# Identity only: which accounts exist, what they are called, what plan they carry.
# Everything in this file is safe to print. There is nowhere here to put a secret.

def default_alias(email: str) -> str:
    return email.split("@")[0].replace(".", "-").replace("_", "-").lower()


def roster_read() -> dict:
    try:
        value = json.loads(ROSTER.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def roster_write(roster: dict) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE, 0o700)
    tmp = ROSTER.with_suffix(".tmp")
    tmp.write_text(json.dumps(roster, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, ROSTER)


def roster_note(account: dict, alias: str | None = None) -> str | None:
    """Record a logged-in account's identity, returning its alias. Idempotent."""
    email = account.get("emailAddress")
    if not email:
        return None
    roster = roster_read()
    alias = alias or next((a for a, e in roster.items() if e.get("email") == email),
                          default_alias(email))
    entry = dict(roster.get(alias) or {})
    org_uuid = (account.get("organizationUuid") or "").strip().lower() or None
    for other, held in roster.items():
        if (other != alias and org_uuid and str(held.get("org_uuid") or "").lower() == org_uuid
                and held.get("email") != email):
            warn(f"roster had org {org_uuid[:8]}… under {held.get('email')}, but {email}'s login "
                 "holds it; cleared from the other entry")
            held.pop("org_uuid", None)
    if org_uuid and entry.get("org_uuid") and entry["org_uuid"].lower() != org_uuid:
        # A login is the truth about its own org; a noted one was evidence. Say so, then correct it.
        warn(f"roster had {email} as org {entry['org_uuid'][:8]}…, but its login says "
             f"{org_uuid[:8]}…; meter readings now follow the login")
    entry.update({
        "email": email,
        "tier": account.get("organizationRateLimitTier") or entry.get("tier"),
        "org": account.get("organizationName") or entry.get("org"),
        "org_uuid": org_uuid or entry.get("org_uuid"),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    })
    entry.setdefault("first_seen", entry["last_seen"])
    roster[alias] = entry
    roster_write(roster)
    return alias


def alias_for(email: str, roster: dict) -> str | None:
    return next((a for a, e in roster.items() if e.get("email") == email), None)


def aliases_for_org(org_uuid: str | None, roster: dict) -> list[str]:
    """Every roster alias with an email whose organization is ORG_UUID. The join a meter reading rides on.

    More than one is ambiguous and is never resolved by guessing: a mistaken `note`, or a
    Team plan whose seats share one organization (org ID cannot tell those seats apart).
    """
    if not org_uuid:
        return []
    wanted = org_uuid.strip().lower()
    return sorted(a for a, e in roster.items()
                  if e.get("email") and str(e.get("org_uuid") or "").lower() == wanted)


# ---------------------------------------------------------------- selection
# Which account a new session starts in. A pointer, not a login: it names an
# email, and the email is looked up among the homes at launch.

def selection_read() -> dict:
    try:
        value = json.loads(SELECTION.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def selected_email(all_homes: list[Home] | None = None) -> str | None:
    """The account new sessions go to: the selection if a home holds it, else the default home's."""
    all_homes = all_homes or homes()
    chosen = selection_read().get("email")
    if chosen and home_for(chosen, all_homes):
        return chosen
    return all_homes[0].email()


def switch_log_append(from_email: str | None, to_email: str, source: str, note: str | None) -> None:
    roster = roster_read()
    from_alias = alias_for(from_email, roster) if from_email else None
    to_alias = alias_for(to_email, roster) or default_alias(to_email)
    record = {"at": now_iso(), "from": from_alias, "to": to_alias, "source": source,
              "usage": {alias: None for alias in (from_alias, to_alias) if alias}}
    if note:
        record["note"] = note
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        with SWITCH_LOG.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass  # the journal must never block a switch


def select(email: str, *, mode: str, source: str, note: str | None = None) -> bool:
    """Point new sessions at EMAIL. Returns True when the selected account changed."""
    previous = selected_email()
    write_json_atomic(SELECTION, {"email": email, "mode": mode, "set_at": now_iso(), "source": source})
    if previous != email:
        switch_log_append(previous, email, source, note)
        return True
    return False


def glideslope_pick() -> dict:
    """Ask Glideslope which account new work should go to. Raises RuntimeError."""
    if not GLIDESLOPE.exists():
        raise RuntimeError(f"Glideslope not found at {GLIDESLOPE}")
    result = subprocess.run(
        [sys.executable, str(GLIDESLOPE), "--pick", "--json"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no detail"
        raise RuntimeError(f"glideslope --pick failed: {detail}")
    try:
        pick = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"glideslope --pick returned invalid JSON: {exc}") from exc
    if not isinstance(pick, dict) or not pick.get("email"):
        raise RuntimeError(pick.get("reason") if isinstance(pick, dict) else "no pick")
    return pick


def resolve_launch_home(requested: str | None) -> Home:
    """The home a new session should start in. Raises RuntimeError with a readable reason."""
    all_homes = homes()
    roster = roster_read()
    name = requested or os.environ.get("CLAUDE_ACCOUNT") or ""
    if name.lower() == "auto" or (not name and selection_read().get("mode") == "auto"):
        pick = glideslope_pick()
        email = pick["email"]
        if not name:  # only the persistent auto mode moves the pointer
            select(email, mode="auto", source="claude-account-auto", note=pick.get("reason"))
    elif name:
        email = resolve_email(name, roster)
        if not email:
            raise RuntimeError(f"unknown account '{name}'")
    else:
        email = selected_email(all_homes)
    home = home_for(email, all_homes)
    if home is None:
        raise RuntimeError(f"no login for {label_for(email, roster)} on this machine — "
                           f"run: claude-account login {label_for(email, roster).lower()}")
    if home.config_dir is not None:
        link_profile(home.config_dir)
    return home


# ---------------------------------------------------------------- usage cache
# Every successful read is journaled here, so an account whose token has lapsed
# still has last-known numbers. No secrets — percentages and reset times only.

def cache_put(alias: str, email: str, rows: list[tuple[str, float, str]],
              extra_usage: dict | None = None) -> None:
    try:
        USAGE_CACHE.mkdir(parents=True, exist_ok=True)
        os.chmod(USAGE_CACHE, 0o700)
        p = USAGE_CACHE / f"{alias}.json"
        tmp = p.with_suffix(".tmp")
        payload = {
            "email": email,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "limits": [{"label": l, "percent": pc, "resets_at": r} for l, pc, r in rows],
        }
        # Absent on a meter-token write, on purpose: that read cannot see the
        # counter, and leaving the previous login's number here would pair it
        # with fresh percents from a different source.
        if extra_usage is not None:
            payload["extra_usage"] = extra_usage
        tmp.write_text(json.dumps(payload, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        pass  # a cache write must never break a status read


def cache_get(alias: str) -> dict | None:
    p = USAGE_CACHE / f"{alias}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def age_str(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:
        return "?"
    if secs < 3600:
        return f"{secs / 60:.0f}m"
    if secs < 86400:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


# ---------------------------------------------------------------- usage

class Throttled(Exception):
    """The usage endpoint answered 429. `retry_after` is its hint in seconds, if any."""

    def __init__(self, retry_after: float | None):
        super().__init__("HTTP Error 429: Too Many Requests")
        self.retry_after = retry_after


def fetch_usage(token: str) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise Throttled(_retry_after_seconds(exc.headers.get("Retry-After"))) from exc
        raise


def _retry_after_seconds(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP-date form is rare here; the default pause covers it


def backoff_until(now: float | None = None) -> float | None:
    """The epoch second before which no usage read may go out, or None when clear."""
    try:
        until = float(json.loads(USAGE_BACKOFF.read_text())["until_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return until if until > (time.time() if now is None else now) else None


def start_backoff(retry_after: float | None, now: float | None = None) -> float:
    seconds = BACKOFF_DEFAULT_S if retry_after is None else retry_after
    seconds = min(max(seconds, BACKOFF_MIN_S), BACKOFF_MAX_S)
    until = (time.time() if now is None else now) + seconds
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        write_json_atomic(USAGE_BACKOFF, {
            "until_epoch": until,
            "until": datetime.fromtimestamp(until, timezone.utc).isoformat(),
            "retry_after": retry_after,
            "set_at": now_iso(),
        })
    except OSError:
        pass  # a pause we cannot record still holds for the rest of this run
    return until


def _throttled_note(until: float) -> str:
    local = datetime.fromtimestamp(until).astimezone().strftime("%H:%M")
    return f"throttled by Anthropic's usage endpoint; next read after {local}"


def limit_rows(usage: dict) -> list[tuple[str, float, str]]:
    """(label, percent, resets_at) for session / weekly-all / each scoped weekly.

    Usage credits are not one of these rows. `parse_extra_usage` reads that
    counter off the same payload.
    """
    rows = []
    for lim in usage.get("limits") or []:
        kind = lim.get("kind")
        if kind == "session":
            label = "session (5h)"
        elif kind == "weekly_all":
            label = "weekly (all models)"
        elif kind == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            label = f"weekly ({model})"
        else:
            label = kind or "?"
        rows.append((label, float(lim.get("percent") or 0), lim.get("resets_at")))
    return rows


def _minor_units(value: object) -> int | None:
    """A non-negative integer count of minor currency units, or None when the field is unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        minor = value
    elif isinstance(value, float) and value.is_integer():
        minor = int(value)
    else:
        return None
    return minor if minor >= 0 else None


def _exponent(value: object) -> int:
    if isinstance(value, bool):
        return 2
    if isinstance(value, int) and 0 <= value <= 8:
        return value
    if isinstance(value, float) and value.is_integer() and 0 <= int(value) <= 8:
        return int(value)
    return 2


def _currency_code(value: object) -> str:
    if isinstance(value, str):
        code = value.strip().upper()
        if 3 <= len(code) <= 8 and code.isalpha():
            return code
    return "USD"


def valid_extra_usage(value: object) -> dict | None:
    """The parsed usage-credit counter, or None when the cache shape is not one.

    `monthly_limit` is deliberately not required: null means the cap is
    unlimited, and dropping the block on that used to hide real spend.
    """
    if not isinstance(value, dict):
        return None
    minor = _minor_units(value.get("used_minor"))
    if minor is None:
        return None
    places = value.get("exponent", 2)
    if isinstance(places, bool) or not isinstance(places, int) or not 0 <= places <= 8:
        places = 2
    return {
        "enabled": bool(value.get("enabled")),
        "used_minor": minor,
        "currency": _currency_code(value.get("currency")),
        "exponent": places,
    }


def parse_extra_usage(usage: dict) -> dict | None:
    """The month-to-date usage-credit counter on one `/api/oauth/usage` payload.

    `spend` wins when it carries an amount, because its exponent is explicit.
    Otherwise `extra_usage.used_credits`, with `decimal_places` or 2. A missing
    block is None — never a fabricated zero. Enabled-but-zero is a real reading:
    the next rise needs a baseline of zero.
    """
    if not isinstance(usage, dict):
        return None
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("used"), dict):
        used = spend["used"]
        minor = used.get("amount_minor")
        if minor is None:
            minor = used.get("amount_minor_units")
        parsed = _minor_units(minor)
        if parsed is not None:
            return valid_extra_usage({
                "enabled": spend.get("enabled"),
                "used_minor": parsed,
                "currency": used.get("currency") or spend.get("currency"),
                "exponent": _exponent(used.get("exponent")),
            })
    extra = usage.get("extra_usage")
    if not isinstance(extra, dict):
        return None
    parsed = _minor_units(extra.get("used_credits"))
    if parsed is None:
        return None
    return valid_extra_usage({
        "enabled": extra.get("is_enabled"),
        "used_minor": parsed,
        "currency": extra.get("currency"),
        "exponent": _exponent(extra.get("decimal_places")),
    })


# ---------------------------------------------------------------- meter tokens
# One account's meters without a login: a 1-token request under an operator-minted
# setup-token, read from the rate-limit headers every response carries. See the
# module docstring for why this exists and PROVIDERS.md for the contract.

_CLI_VERSION: list[str] = []  # memo: the installed Claude Code version, found once per run


def cli_version() -> str:
    """The installed Claude Code's version. Newer models refuse an older client identity."""
    if _CLI_VERSION:
        return _CLI_VERSION[0]
    version = UA.split("/")[1].split()[0]
    for candidate in CLI_CANDIDATES:
        if not os.access(candidate, os.X_OK):
            continue
        try:
            out = subprocess.run([str(candidate), "--version"], capture_output=True, text=True,
                                 timeout=5, check=False).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out and out[0][:1].isdigit():
            version = out[0]
            break
    _CLI_VERSION.append(version)
    return version


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A bearer token is never carried to wherever a redirect points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def meter_token_files() -> list[Path]:
    try:
        return sorted(p for p in METER_TOKENS.iterdir() if p.suffix == ".token" and p.is_file())
    except OSError:
        return []


def read_token_file(path: Path) -> str:
    """The token in a token file: its last line that is not blank and not a '#' comment."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise RuntimeError(f"{path.name} is not UTF-8 text") from None
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise RuntimeError(f"{path.name} holds no token")
    token = lines[-1]
    if not (token.isascii() and token.isprintable() and " " not in token):
        raise RuntimeError(f"{path.name}: the token line has non-ASCII, invisible or space characters")
    return token


def token_fingerprint(token: str) -> str:
    """Safe to print: enough to tell two tokens apart, useless for anything else."""
    return hashlib.sha256(token.encode()).hexdigest()[:10]


def safe_error(exc: BaseException) -> str:
    """This module's own messages never carry a token; anything unexpected is named, not quoted."""
    return str(exc) if isinstance(exc, RuntimeError) else f"unexpected {type(exc).__name__}"


def meter_rows(headers) -> list[dict]:
    """The unified rate-limit headers → limit rows in the usage read's shape."""
    rows = []
    for claim, label in METER_CLAIMS.items():
        try:
            fraction = float(headers.get(f"anthropic-ratelimit-unified-{claim}-utilization"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(fraction) or fraction < 0:
            continue
        try:
            reset = int(float(headers.get(f"anthropic-ratelimit-unified-{claim}-reset")))
            resets_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            resets_at = None
        rows.append({"label": label, "percent": round(fraction * 100, 1), "resets_at": resets_at})
    return rows


def probe_token(token: str, *, identity_only: bool = False) -> tuple[str, list[dict], str | None]:
    """(organization ID, limit rows, why Fable is missing or None) for the account TOKEN bills.

    A 429 still carries the headers, so an account at its limit reads as at its limit;
    a 429 without them is a failed read, never a cue to send another request. 401 is a
    dead token. Only a refusal of the MODEL (400, 403, 404 on the Fable request: a plan
    without Fable, a client too old for it) moves on to Haiku, which answers 5h and weekly
    but not Fable; a 5xx or a network failure is a failed read, and the caller serves the
    last one it has.

    IDENTITY_ONLY (for `whose`, which needs the organization, not the Fable meter): any
    failure but a dead token moves on to the next model, so an overloaded Fable (529)
    cannot block a verification.
    """
    fallback = None
    for index, model in enumerate(METER_MODELS):
        last = index == len(METER_MODELS) - 1
        body = json.dumps({
            "model": model, "max_tokens": 1,
            "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
            "messages": [{"role": "user", "content": "."}],
        }).encode()
        req = urllib.request.Request(MESSAGES_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "User-Agent": f"claude-cli/{cli_version()} (external, cli)",
        })
        try:
            with _open(req, METER_TIMEOUT_S) as response:
                headers = response.headers
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 401 or (code == 403 and last):
                raise RuntimeError(f"meter token rejected (HTTP {code}): revoked or not a setup-token") from None
            if not last and (code in (400, 403, 404) or (identity_only and code != 429)):
                fallback = f"{model} refused (HTTP {code})" if code in (400, 403, 404) else f"{model} failed (HTTP {code})"
                continue
            if code != 429:
                raise RuntimeError(f"meter probe failed: HTTP {code} on {model}") from None
            headers = exc.headers
            if not meter_rows(headers or {}):
                raise RuntimeError(f"meter probe throttled (HTTP 429) with no meter headers") from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            if identity_only and not last:
                fallback = f"{model} failed ({type(exc).__name__})"
                continue
            raise RuntimeError(f"meter probe failed: {type(exc).__name__}") from None
        org = (headers.get("anthropic-organization-id") or "").strip().lower()
        rows = meter_rows(headers)
        if not org or not rows:
            raise RuntimeError(f"meter probe failed: {model} answered without meter headers")
        return org, rows, fallback
    raise RuntimeError(f"meter probe failed: {fallback or 'no model answered'}")


def _meter_orgs() -> dict:
    """Token fingerprint → the org it last billed. Lets a run skip tokens a login already covers."""
    try:
        value = json.loads(METER_ORGS.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def meter_reads(roster: dict, want: set | None = None) -> tuple[dict[str, dict], list[str]]:
    """Probe meter tokens → ({alias: reading}, notes). Unknown and ambiguous orgs are noted, never credited.

    With WANT, a token already known to bill an account outside it (one a login here
    just read live) is not probed at all: a probe is spent only where it is the only read.
    """
    reads: dict[str, dict] = {}
    notes: list[str] = []
    known = _meter_orgs()
    deadline = time.monotonic() + METER_BUDGET_S
    for path in meter_token_files():
        try:
            token = read_token_file(path)
        except Exception as exc:  # one bad file must not cost the other accounts their reads
            notes.append(f"meter token {path.name}: {safe_error(exc)}")
            continue
        fingerprint = token_fingerprint(token)
        covered = aliases_for_org(known.get(fingerprint), roster)
        if want is not None and len(covered) == 1 and covered[0] not in want:
            continue
        if time.monotonic() > deadline:
            notes.append(f"meter token {path.name}: not read, this run's {METER_BUDGET_S:.0f}s meter budget is spent")
            continue
        try:
            org, rows, fallback = probe_token(token)
        except Exception as exc:
            notes.append(f"meter token {path.name}: {safe_error(exc)}")
            continue
        known[fingerprint] = org
        owners = aliases_for_org(org, roster)
        if len(owners) != 1:
            notes.append(f"meter token {path.name} ({fingerprint}) bills org {org}, which "
                         + ("no roster entry names" if not owners else f"{len(owners)} roster entries share")
                         + "; its reading is dropped (claude-account note <email> --org <id>)")
            continue
        alias = owners[0]
        email = roster[alias]["email"]
        named = resolve_email(path.stem, roster)
        if named and named.lower() != email.lower():
            notes.append(f"meter token {path.name} is named for {label_for(named, roster)} but bills "
                         f"{label_for(email, roster)}; the reading is credited to {label_for(email, roster)}")
        if fallback:
            notes.append(f"meter token {path.name}: {fallback}, so {label_for(email, roster)}'s Fable weekly "
                         "is unread this run")
        if alias in reads:
            notes.append(f"meter tokens {reads[alias]['read_via']} and {path.name} bill the same account; "
                         f"reading {path.name}")
        reads[alias] = {"org_uuid": org, "rows": rows, "read_via": path.name, "fetched_at": now_iso()}
    try:
        write_json_atomic(METER_ORGS, known)
    except OSError:
        pass  # a skipped probe next run is the only cost
    return reads, notes


# ---------------------------------------------------------------- presentation

def local_time(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %b %d %H:%M %Z")
    except Exception:
        return iso


def bar(pct: float, width: int = 18) -> str:
    filled = int(round(pct / 100 * width))
    color = C["green"] if pct < 70 else C["yellow"] if pct < 95 else C["red"]
    return f"{color}{'█' * filled}{C['dim']}{'·' * (width - filled)}{C['off']}"


# ---------------------------------------------------------------- commands

def cmd_which(_args: list[str]) -> None:
    all_homes = homes()
    email = selected_email(all_homes)
    if not email:
        print("not logged in")
        return
    held = [home.name for home in all_homes if home.email() == email]
    mode = selection_read().get("mode") or "default"
    print(f"{C['bold']}{label_for(email)}{C['off']}  {email}  "
          f"{C['dim']}new sessions · {mode} · home {', '.join(held) or '—'}{C['off']}")


def cmd_homes(args: list[str]) -> None:
    all_homes = homes()
    chosen = selected_email(all_homes)
    rows = []
    for home in all_homes:
        email = home.email()
        state, hours = token_state(home)
        rows.append({
            "home": home.name, "config_dir": home.env_value() or None, "email": email,
            "call_sign": call_sign_for(email), "keychain_service": home.keychain_service,
            "token": state, "token_hours_left": round(hours, 2) if hours is not None else None,
            "selected": bool(email) and email == chosen,
        })
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        mark = f"{C['green']}▶{C['off']}" if row["selected"] else " "
        token = (f"{C['green']}token {row['token_hours_left']:.1f}h{C['off']}" if row["token"] == "valid"
                 else f"{C['yellow']}token expired{C['off']}" if row["token"] == "expired"
                 else f"{C['dim']}no login{C['off']}")
        who = f"{row['call_sign'] or '?'} · {row['email']}" if row["email"] else "—"
        print(f"{mark} {C['bold']}{row['home']:<10}{C['off']} {who:<44} {token}  "
              f"{C['dim']}{row['config_dir'] or '~/.claude'}{C['off']}")


def cmd_use(args: list[str]) -> None:
    if not args:
        die("usage: claude-account use <account|auto>")
    if args[0].lower() == "auto":
        try:
            pick = glideslope_pick()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            die(str(exc))
        changed = select(pick["email"], mode="auto", source="claude-account-auto", note=pick.get("reason"))
        verb = "switched to" if changed else "staying on"
        print(f"auto · {verb} {C['bold']}{label_for(pick['email'])}{C['off']}  "
              f"{C['dim']}{pick.get('reason', '')}{C['off']}")
        return
    roster = roster_read()
    email = resolve_email(args[0], roster)
    if not email:
        die(f"unknown account '{args[0]}'. try a call-sign ({', '.join(CALL_SIGNS.values())}) or an alias")
    home = home_for(email)
    if home is None:
        die(f"no login for {label_for(email, roster)} on this machine — "
            f"run: claude-account login {label_for(email, roster).lower()}")
    entry = roster.get(alias_for(email, roster) or "", {})
    if entry.get("dormant"):
        warn(f"{label_for(email, roster)} is marked dormant in the roster; routing to it anyway")
    changed = select(email, mode="fixed", source="claude-account-use")
    verb = "new sessions now use" if changed else "already on"
    print(f"{verb} {C['bold']}{label_for(email, roster)}{C['off']}  "
          f"{C['dim']}home {home.name}{C['off']}")


def cmd_home(args: list[str]) -> None:
    try:
        home = resolve_launch_home(args[0] if args else None)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        die(str(exc))
    print(home.env_value())


def cmd_exec(args: list[str]) -> None:
    """Run a command under an account's home. Never refuses to launch.

    If the account cannot be resolved it warns and runs the command with the
    environment it was given — a routing hiccup must not cost anyone a session.
    """
    if "--" not in args:
        die("usage: claude-account exec [account] -- <command…>")
    split = args.index("--")
    requested, command = (args[:split] or [None])[0], args[split + 1:]
    if not command:
        die("usage: claude-account exec [account] -- <command…>")
    env = dict(os.environ)
    try:
        home = resolve_launch_home(requested)
        if home.config_dir is None:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(home.config_dir)
        env["CLAUDE_ACCOUNT_HOME"] = home.name
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        warn(f"{exc}; launching with the current environment")
    env.pop("CLAUDE_ACCOUNT", None)  # a one-launch override must not leak into children
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        die(f"could not run {command[0]}: {exc}")


def cmd_login(args: list[str]) -> None:
    if not args:
        die("usage: claude-account login <account|name>")
    roster = roster_read()
    email = resolve_email(args[0], roster)
    existing = home_for(email) if email else None
    if existing is not None:
        print(f"{label_for(email, roster)} is already logged in (home {existing.name})")
        return
    name = (call_sign_for(email) or args[0]).lower().replace("@", "-at-")
    profile_dir = ensure_profile(name)
    print(f"login home ready: {profile_dir}")
    watcher = GLIDESLOPE.parent / "tools" / "install-login-watch.sh"
    if watcher.exists():  # WatchPaths cannot glob — re-arm it so this home's login is seen at once
        subprocess.run([str(watcher)], capture_output=True, check=False)
    print(f"opening Claude Code there — type {C['bold']}/login{C['off']} and sign in"
          + (f" as {email}" if email else ""))
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(profile_dir))
    env.pop("CLAUDE_ACCOUNT", None)
    os.execvpe("claude", ["claude"], env)


def cmd_link(_args: list[str]) -> None:
    for home in homes():
        if home.config_dir is None:
            continue
        linked = link_profile(home.config_dir)
        print(f"{home.name}: " + (f"linked {', '.join(linked)}" if linked else "up to date"))


def cmd_save(args: list[str]) -> None:
    saved = []
    for home in homes():
        account = home.account()
        if account.get("emailAddress"):
            alias = roster_note(account, args[0] if args and home.is_default else None)
            saved.append(f"{account['emailAddress']} as '{alias}' ({home.name})")
    if not saved:
        die("no home holds a login -- log in first")
    for line in saved:
        print(f"{C['green']}recorded{C['off']} {line} {C['dim']}(identity only — no credentials){C['off']}")


def _flag(args: list[str], name: str) -> tuple[str | None, list[str]]:
    """(value of --name, the other arguments). A flag given without a value is a usage error."""
    if name not in args:
        return None, list(args)
    i = args.index(name)
    if i + 1 >= len(args) or not args[i + 1].strip() or args[i + 1].startswith("--"):
        die(f"{name} needs a value")
    return args[i + 1], args[:i] + args[i + 2:]


def cmd_note(args: list[str]) -> None:
    """Record an account's identity without a login here, so a meter token can be joined to it.

    Evidence, not a guess: take the org ID from that account's own login record
    (`claude auth status --json` in a home holding it) or from `whose` on a token whose
    account is otherwise established. A later login here corrects a wrong note, loudly.
    """
    org, rest = _flag(args, "--org")
    alias, rest = _flag(rest, "--alias")
    email = rest[0].strip().lower() if rest and "@" in rest[0] else None
    if not email or not org:
        die("usage: claude-account note <email> --org <organization-uuid> [--alias <alias>]")
    org = org.strip().lower()
    roster = roster_read()
    alias = alias or alias_for(email, roster) or default_alias(email)
    holders = [a for a in aliases_for_org(org, roster) if roster[a].get("email", "").lower() != email]
    if holders:
        die(f"org {org[:8]}… already belongs to {roster[holders[0]].get('email')} ('{holders[0]}')")
    if alias in roster and str(roster[alias].get("email") or email).lower() != email:
        die(f"alias '{alias}' already names {roster[alias].get('email')}; pass --alias")
    entry = dict(roster.get(alias) or {})
    entry.update({"email": email, "org_uuid": org})
    entry.setdefault("first_seen", datetime.now(timezone.utc).isoformat())
    roster[alias] = entry
    roster_write(roster)
    print(f"{C['green']}noted{C['off']} {email} as '{alias}', org {org} "
          f"{C['dim']}(identity only — no credentials){C['off']}")


def cmd_whose(args: list[str]) -> None:
    expect, rest = _flag(args, "--expect")
    as_json = "--json" in rest
    files = [Path(a).expanduser() for a in rest if a != "--json"] or meter_token_files()
    if not files:
        die(f"no token files given and none in {METER_TOKENS}")
    roster = roster_read()
    expected = resolve_email(expect, roster) if expect else None
    if expect and not expected:
        die(f"unknown account '{expect}'. try a call-sign or an alias")
    results, worst = [], 0
    for path in files:
        row: dict = {"file": str(path), "org_uuid": None, "email": None, "call_sign": None,
                     "alias": None, "limits": [], "verified": False}
        try:
            token = read_token_file(path)
            row["fingerprint"] = token_fingerprint(token)
            org, rows, fallback = probe_token(token, identity_only=True)
        except Exception as exc:
            row["error"] = safe_error(exc)
            worst = max(worst, 2)
            results.append(row)
            continue
        owners = aliases_for_org(org, roster)
        alias = owners[0] if len(owners) == 1 else None
        email = roster[alias]["email"] if alias else None
        row.update({"org_uuid": org, "alias": alias, "email": email, "limits": rows,
                    "call_sign": call_sign_for(email) if email else None,
                    "observed_at": now_iso()})
        if fallback:
            row["note"] = f"{fallback}; Fable weekly unread"
        if email is None:
            row["error"] = (f"bills org {org}, which no roster entry names" if not owners
                            else f"bills org {org}, which {len(owners)} roster entries share ({', '.join(owners)})")
            worst = max(worst, 4)
        elif expected and email.lower() != expected.lower():
            row["error"] = f"bills {label_for(email, roster)}, not {label_for(expected, roster)}"
            worst = max(worst, 3)
        else:
            row["verified"] = True
        results.append(row)
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            who = (f"{row['call_sign'] or row['alias']} · {row['email']}" if row["email"]
                   else f"org {row['org_uuid']}" if row["org_uuid"] else "—")
            mark = f"{C['green']}✓{C['off']}" if row["verified"] else f"{C['red']}✗{C['off']}"
            print(f"{mark} {C['bold']}{Path(row['file']).name}{C['off']}  {who}  "
                  f"{C['dim']}{row.get('fingerprint', '')}{C['off']}")
            for key, color in (("error", "red"), ("note", "yellow")):
                if row.get(key):
                    print(f"  {C[color]}{row[key]}{C['off']}")
            for lim in row["limits"]:
                print(f"  {lim['label']:<21} {bar(lim['percent'])} {lim['percent']:5.1f}%   "
                      f"{C['dim']}resets {local_time(lim['resets_at'])}{C['off']}")
    sys.exit(worst)


def cmd_remove(args: list[str]) -> None:
    if not args:
        die("usage: claude-account remove <alias>")
    roster = roster_read()
    if args[0] not in roster:
        die(f"no account '{args[0]}' in the roster")
    del roster[args[0]]
    roster_write(roster)
    print(f"removed '{args[0]}' from the roster {C['dim']}(the account itself is untouched){C['off']}")


def cmd_list(_args: list[str]) -> None:
    roster = roster_read()
    if not roster:
        print("roster is empty -- run: claude-account save")
        return
    logged_in = {home.email() for home in homes()} - {None}
    chosen = selected_email()
    for alias, entry in sorted(roster.items()):
        email = entry.get("email")
        mark = (f"{C['green']}▶{C['off']}" if email == chosen
                else f"{C['green']}●{C['off']}" if email in logged_in else f"{C['dim']}○{C['off']}")
        print(f"{mark} {C['bold']}{alias:<14}{C['off']} {email or '?':<34} "
              f"{C['dim']}{entry.get('tier','?')}{C['off']}")


def cmd_status(args: list[str]) -> None:
    """Live numbers for every account a home here holds; a meter token's read for the rest; last known after that.

    `active` keeps its meaning for every reader: the account new sessions on this
    machine go to. `logged_in` is the wider fact — a home here holds the account.
    Login reads come first; a meter token is probed only for an account they left unread.
    """
    as_json = "--json" in args
    all_homes = homes()
    for home in all_homes:
        if home.email():
            roster_note(home.account())   # the roster keeps itself
    roster = roster_read()
    chosen = selected_email(all_homes)
    report: dict[str, dict] = {}
    blocks: dict[str, list[str]] = {}  # the text view, per account, printed in roster order
    paused_until = backoff_until()
    reads = 0

    def lines(rows: list[dict]) -> list[str]:
        return [f"  {r['label']:<21} {bar(r['percent'])} {r['percent']:5.1f}%   "
                f"{C['dim']}resets {local_time(r['resets_at'])}{C['off']}" for r in rows]

    bases: dict[str, dict] = {}
    for alias, entry in sorted(roster.items()):
        email = entry.get("email", "?")
        home = home_for(email, all_homes)
        is_active = email == chosen
        base = bases[alias] = {"email": email, "active": is_active, "logged_in": home is not None,
                               "home": home.name if home else None}
        if home is None:
            continue
        if paused_until is not None:
            report[alias] = {**base, "stale": True, "error": _throttled_note(paused_until)}
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {email}  {C['dim']}home {home.name}{C['off']}",
                             f"  {C['yellow']}{_throttled_note(paused_until)}{C['off']}"]
            continue
        try:
            if reads:
                time.sleep(READ_SPACING_S)
            reads += 1
            usage = fetch_usage(live_token(home))
            rows = limit_rows(usage)
            extra = parse_extra_usage(usage)
            cache_put(alias, email, rows, extra)
            limits = [{"label": l, "percent": p, "resets_at": r} for l, p, r in rows]
            report[alias] = {**base, "limits": limits, "fetched_at": now_iso()}
            if extra is not None:
                report[alias]["extra_usage"] = extra
            tag = f"{C['green']}▶ new sessions{C['off']}" if is_active else f"{C['green']}● live{C['off']}"
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {C['cyan']}{email}{C['off']}  {tag}  "
                             f"{C['dim']}home {home.name}{C['off']}"] + lines(limits)
        except Throttled as ex:
            paused_until = start_backoff(ex.retry_after)
            report[alias] = {**base, "stale": True, "error": _throttled_note(paused_until)}
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {email}  {C['dim']}home {home.name}{C['off']}",
                             f"  {C['yellow']}{_throttled_note(paused_until)}{C['off']}"]
        except Exception as ex:
            report[alias] = {**base, "stale": True, "error": str(ex)}
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {email}  {C['dim']}home {home.name}{C['off']}",
                             f"  {C['red']}unavailable:{C['off']} {ex}"]

    unread = {alias for alias in bases if alias not in report or report[alias].get("stale")}
    try:
        meters, meter_notes = meter_reads(roster, want=unread)
    except Exception as exc:  # the meter must never cost the login reads
        meters, meter_notes = {}, [f"meter tokens not read: {safe_error(exc)}"]
    for note in meter_notes:
        warn(note)

    for alias in sorted(unread):
        base, email = bases[alias], bases[alias]["email"]
        meter = meters.get(alias)
        if meter is not None:
            # As fresh as a home read, and it needs no login: `logged_in` keeps saying
            # whether a home here holds the account, `source` says how it was read.
            cache_put(alias, email, [(r["label"], r["percent"], r["resets_at"]) for r in meter["rows"]])
            report[alias] = {**base, "limits": meter["rows"], "fetched_at": meter["fetched_at"],
                             "source": "meter-token", "read_via": meter["read_via"],
                             "org_uuid": meter["org_uuid"]}
            tag = f"{C['green']}▶ new sessions{C['off']}" if base["active"] else f"{C['green']}◆ meter token{C['off']}"
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {C['cyan']}{email}{C['off']}  {tag}  "
                             f"{C['dim']}{meter['read_via']}{C['off']}"] + lines(meter["rows"])
            continue
        cached = cache_get(alias)
        if cached and cached.get("limits"):
            report[alias] = {
                **base, "stale": True,
                "fetched_at": cached.get("fetched_at"),
                "error": report.get(alias, {}).get("error", "not logged in on this machine"),
                "limits": cached["limits"],
            }
            extra = valid_extra_usage(cached.get("extra_usage"))
            if extra is not None:
                report[alias]["extra_usage"] = extra
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {C['cyan']}{email}{C['off']}  "
                             f"{C['dim']}○ last known ({age_str(cached.get('fetched_at'))} ago){C['off']}"
                             ] + lines(cached["limits"])
        elif alias not in report:
            # limits: [] — Glideslope rejects a snapshot with any account lacking the list
            report[alias] = {**base, "stale": True, "limits": [], "error": "never read while logged in"}
            blocks[alias] = [f"\n{C['bold']}{alias}{C['off']}  {email}",
                             f"  {C['dim']}never read while logged in{C['off']}"]

    if as_json:
        print(json.dumps(report, indent=2))
        return
    if not roster:
        die("roster is empty -- run: claude-account save")
    for alias in sorted(blocks):
        print("\n".join(blocks[alias]))
    print(f"\n{C['dim']}switch new sessions with: claude-account use <account|auto>{C['off']}")


COMMANDS = {
    "status": cmd_status, "homes": cmd_homes, "use": cmd_use, "home": cmd_home,
    "exec": cmd_exec, "login": cmd_login, "link": cmd_link, "list": cmd_list,
    "which": cmd_which, "save": cmd_save, "remove": cmd_remove, "note": cmd_note,
    "whose": cmd_whose,
}


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    CALL_SIGNS.update(call_signs_by_email())
    cmd = argv[0] if argv and not argv[0].startswith("-") else "status"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    fn = COMMANDS.get(cmd)
    if not fn:
        die(f"unknown command '{cmd}'. try: {', '.join(COMMANDS)}")
    fn(rest)


if __name__ == "__main__":
    main()
