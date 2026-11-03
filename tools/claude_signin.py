#!/usr/bin/env python3
"""Sign a Claude account in on this machine: the browser half only, no terminal.

    claude_signin.py <account>             sign in to the account's own login home
    claude_signin.py --default <account>   sign in to the shared default home (~/.claude):
                                           every session there follows (the global switch)

Runs `claude auth login --claudeai --email <email>` in the target home, detached, and
returns at once: Claude Code opens claude.ai in the default browser with the account's
email filled in, waits for the approval on its own localhost callback, and writes the
login itself. A watcher then reads which account the home now holds and posts one
banner through Glideslope's notify sink: signed in, signed in as the wrong account, or
not finished. Claude Code stays the only process that ever writes or refreshes a token;
this reads the home's login record (`oauthAccount`) and nothing else.

Built for the popup's Sign in and Move all buttons, through the host's manifest actions
(tools/claude-login-pane). The approval in the browser is always a human's, or an
agent's driving a browser a human signed in.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_account as ca  # noqa: E402

SIGNIN_DIR = ca.STORE / "signin"
SIGNIN_TIMEOUT_S = 15 * 60   # a browser approval left this long is abandoned


def machine_name() -> str:
    try:
        import glideslope
        return glideslope.LOCAL_SATELLITE
    except Exception:  # noqa: BLE001
        import socket
        return socket.gethostname().split(".")[0] or "this machine"


def notify(title: str, body: str) -> None:
    try:
        import notify as glide_notify
        glide_notify.deliver(title, body)
    except Exception:  # noqa: BLE001 — a lost banner never fails the sign-in
        pass


def target(account: str, default: bool) -> tuple[str, str | None, str, ca.Home]:
    """(login id, email, display name, home) for ACCOUNT. Creates a profile home when none holds it."""
    ca.CALL_SIGNS.update(ca.call_signs_by_login())
    roster = ca.roster_read()
    try:
        login = ca.resolve_exact_login(account, roster)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    email = ca.split_login(login)[0] or None
    display = ca.call_sign_for(login) or account
    if default:
        return login, email, display, ca.Home("default", None)
    # A home already holding the account signs in again in place, the default home included:
    # `use` and `exec` pick that home first, so a second one would never be used.
    held = ca.home_for(login)
    if held is not None:
        return login, email, display, held
    name = display.lower().replace("@", "-at-").replace("#", "-")
    home = ca.Home(name, ca.ensure_profile(name))
    watcher = ROOT / "tools" / "install-login-watch.sh"
    if watcher.exists():  # WatchPaths cannot glob: re-arm so this home's login is seen at once
        subprocess.run([str(watcher)], capture_output=True, check=False)
    return login, email, display, home


# A host action runs under a bare `zsh -c` with launchd's PATH, so look where installers put it.
EXTRA_PATH = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def search_path() -> str:
    """The installers' directories first: the real Claude Code binary, never a multiplexer's wrapper."""
    parts = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
    return os.pathsep.join(EXTRA_PATH + [p for p in parts if p not in EXTRA_PATH])


def claude_binary() -> str:
    found = shutil.which("claude", path=search_path())
    if found is None:
        raise SystemExit("Claude Code (claude) is not installed where this can find it")
    return found


# GLIDESLOPE_BROWSER names the command that receives the claude.ai URL, for an agent that
# completes the approval in a browser it drives; unset, Claude Code opens the system browser.
OPENER = """#!/bin/sh
exec ${GLIDESLOPE_BROWSER:-/usr/bin/open} "$1"
"""


def opener_env(env: dict[str, str]) -> dict[str, str]:
    if not os.environ.get("GLIDESLOPE_BROWSER"):
        return env
    bin_dir = SIGNIN_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    opener = bin_dir / "open"
    opener.write_text(OPENER)
    opener.chmod(0o700)
    env.update(PATH=f"{bin_dir}{os.pathsep}{env['PATH']}", BROWSER=str(opener))
    return env


def home_env(home: ca.Home) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_ACCOUNT", "CLAUDE_ACCOUNT_HOME")}
    env["PATH"] = search_path()
    if home.config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(home.config_dir)
    return env


def start(account: str, default: bool) -> int:
    login, email, display, home = target(account, default)
    claude_binary()  # fail now, in the action's own output, rather than in the detached half
    SIGNIN_DIR.mkdir(parents=True, exist_ok=True)
    log = SIGNIN_DIR / f"{home.name}.log"
    cmd = [sys.executable, str(Path(__file__).resolve()), "--wait", account]
    if default:
        cmd.insert(2, "--default")
    with open(log, "ab") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, cwd=str(Path.home()))
    where = "the shared default home" if default else f"login home {home.name}"
    print(f"opened claude.ai to sign in {display} ({where})")
    return 0


def wait(account: str, default: bool) -> int:
    """The detached half: run the login, then say what landed."""
    login, email, display, home = target(account, default)
    lock_path = SIGNIN_DIR / f"{home.name}.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"{time.strftime('%c')}: a sign-in for {home.name} is already waiting", flush=True)
            notify("Glideslope", f"A {display} sign-in is already waiting in the browser.")
            return 1
        before = home.login()
        cmd = [claude_binary(), "auth", "login", "--claudeai"] + (["--email", email] if email else [])
        print(f"{time.strftime('%c')}: {' '.join(cmd)} in {home.name}", flush=True)
        try:
            result = subprocess.run(cmd, env=opener_env(home_env(home)), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=SIGNIN_TIMEOUT_S)
            finished, code, said = result.returncode == 0, result.returncode, result.stderr or result.stdout
        except subprocess.TimeoutExpired:
            finished, code, said = False, "timeout", ""
        have = home.login()
        print(f"{time.strftime('%c')}: exit={code} holds={ca.split_login(have)[0] or '-'}", flush=True)
        if said and not finished:
            print("claude said: " + " ".join(said.split())[-400:], flush=True)
        landed = finished or have != before
        if have and landed and ca.same_login(have, login):
            ca.roster_note(home.account())
            notify("Glideslope", f"{display} is signed in on {machine_name()}"
                   + (": every session in the shared home follows." if default else "."))
            return 0
        if have and landed and not ca.same_login(have, login):
            notify("Glideslope", f"That sign-in landed as {ca.label_for(have)}, not {display}. "
                   f"Sign claude.ai in as {email or display} and try again.")
            return 3
        notify("Glideslope", f"{display} sign-in did not finish"
               + (" (no approval within 15 minutes)." if not finished else "."))
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--default", action="store_true", help="sign in to the shared default home")
    parser.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("account")
    args = parser.parse_args()
    return wait(args.account, args.default) if args.wait else start(args.account, args.default)


if __name__ == "__main__":
    sys.exit(main())
