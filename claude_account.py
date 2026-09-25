#!/usr/bin/env python3
"""claude-account — which Claude account new sessions use, and what each one has left.

For anyone who runs several Claude accounts and moves between them as limits burn down.

THIS TOOL HOLDS NO CREDENTIALS AND MINTS NO TOKENS.

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
    remove <alias>           drop an account from the roster

An <account> is a call-sign (Bravo), a roster alias (personal) or an email.
CLAUDE_ACCOUNT=<account> overrides the selection for one launch.
"""

from __future__ import annotations

import hashlib
import json
import os
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
SELECTION = STORE / "selection.json"      # which account new sessions use — no token
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
        with path.open("rb") as handle:
            table = tomllib.load(handle).get("claude", {}).get("call_signs", {})
    except (OSError, ValueError, AttributeError, ModuleNotFoundError):
        return {}
    return {str(k): str(v) for k, v in table.items()} if isinstance(table, dict) else {}


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
    entry.update({
        "email": email,
        "tier": account.get("organizationRateLimitTier") or entry.get("tier"),
        "org": account.get("organizationName") or entry.get("org"),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    })
    entry.setdefault("first_seen", entry["last_seen"])
    roster[alias] = entry
    roster_write(roster)
    return alias


def alias_for(email: str, roster: dict) -> str | None:
    return next((a for a, e in roster.items() if e.get("email") == email), None)


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

def cache_put(alias: str, email: str, rows: list[tuple[str, float, str]]) -> None:
    try:
        USAGE_CACHE.mkdir(parents=True, exist_ok=True)
        os.chmod(USAGE_CACHE, 0o700)
        p = USAGE_CACHE / f"{alias}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "email": email,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "limits": [{"label": l, "percent": pc, "resets_at": r} for l, pc, r in rows],
        }, indent=2))
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

def fetch_usage(token: str) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def limit_rows(usage: dict) -> list[tuple[str, float, str]]:
    """(label, percent, resets_at) for session / weekly-all / each scoped weekly."""
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
    """Live numbers for every account a home here holds; last known for the rest.

    `active` keeps its meaning for every reader: the account new sessions on this
    machine go to. `logged_in` is the wider fact — a home here holds the account.
    """
    as_json = "--json" in args
    all_homes = homes()
    for home in all_homes:
        if home.email():
            roster_note(home.account())   # the roster keeps itself
    roster = roster_read()
    chosen = selected_email(all_homes)
    report: dict[str, dict] = {}

    for alias, entry in sorted(roster.items()):
        email = entry.get("email", "?")
        home = home_for(email, all_homes)
        is_active = email == chosen
        base = {"email": email, "active": is_active, "logged_in": home is not None,
                "home": home.name if home else None}
        if home is not None:
            try:
                rows = limit_rows(fetch_usage(live_token(home)))
                cache_put(alias, email, rows)
                report[alias] = {**base,
                                 "limits": [{"label": l, "percent": p, "resets_at": r} for l, p, r in rows]}
                if not as_json:
                    tag = f"{C['green']}▶ new sessions{C['off']}" if is_active else f"{C['green']}● live{C['off']}"
                    print(f"\n{C['bold']}{alias}{C['off']}  {C['cyan']}{email}{C['off']}  {tag}  "
                          f"{C['dim']}home {home.name}{C['off']}")
                    for label, pct, resets in rows:
                        print(f"  {label:<21} {bar(pct)} {pct:5.1f}%   "
                              f"{C['dim']}resets {local_time(resets)}{C['off']}")
                continue
            except Exception as ex:
                report[alias] = {**base, "stale": True, "error": str(ex)}
                if not as_json:
                    print(f"\n{C['bold']}{alias}{C['off']}  {email}  {C['dim']}home {home.name}{C['off']}")
                    print(f"  {C['red']}unavailable:{C['off']} {ex}")
                # fall through so a cached read can still fill the numbers in
        cached = cache_get(alias)
        if cached and cached.get("limits"):
            report[alias] = {
                **base, "stale": True,
                "fetched_at": cached.get("fetched_at"),
                "error": report.get(alias, {}).get("error", "not logged in on this machine"),
                "limits": cached["limits"],
            }
            if not as_json:
                print(f"\n{C['bold']}{alias}{C['off']}  {C['cyan']}{email}{C['off']}  "
                      f"{C['dim']}○ last known ({age_str(cached.get('fetched_at'))} ago){C['off']}")
                for lim in cached["limits"]:
                    print(f"  {lim['label']:<21} {bar(lim['percent'])} {lim['percent']:5.1f}%   "
                          f"{C['dim']}resets {local_time(lim['resets_at'])}{C['off']}")
        elif alias not in report:
            # limits: [] — Glideslope rejects a snapshot with any account lacking the list
            report[alias] = {**base, "stale": True, "limits": [], "error": "never read while logged in"}
            if not as_json:
                print(f"\n{C['bold']}{alias}{C['off']}  {email}")
                print(f"  {C['dim']}never read while logged in{C['off']}")

    if as_json:
        print(json.dumps(report, indent=2))
        return
    if not roster:
        die("roster is empty -- run: claude-account save")
    print(f"\n{C['dim']}switch new sessions with: claude-account use <account|auto>{C['off']}")


COMMANDS = {
    "status": cmd_status, "homes": cmd_homes, "use": cmd_use, "home": cmd_home,
    "exec": cmd_exec, "login": cmd_login, "link": cmd_link, "list": cmd_list,
    "which": cmd_which, "save": cmd_save, "remove": cmd_remove,
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
