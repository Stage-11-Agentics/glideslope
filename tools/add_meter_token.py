#!/usr/bin/env python3
"""Give a Claude account a meter token, so its numbers stay live while nobody is logged in to it.

    add-meter-token <account>                 mint, verify and place it on the meter keeper
    add-meter-token <account> --to <host>     place it on another ssh host (or `local`)

The operator mints the token: this runs `claude setup-token`, which opens claude.ai in the
browser with the account's email filled in, and the approval there is the operator's (or an
agent's driving a browser the operator signed in). What this helper adds is the handling the
operator would otherwise do by hand, without the token ever being shown:

1. capture the token from setup-token's own output (a pseudo-terminal, never echoed);
2. verify it bills the named account (`claude-account whose --expect`, one 1-token request),
   and drop it if it bills another;
3. place it on the keeper as `~/.claude/accounts/meter-tokens/<account>.token`, mode 0600,
   over ssh stdin, and teach the keeper's roster the account (`claude-account note`);
4. copy it into Bitwarden when the vault is unlocked (`BW_SESSION`), else say so.

No copy is left on this machine: the verification file lives in a private temporary
directory that is removed before this exits. See PROVIDERS.md, Meter tokens.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_account as ca  # noqa: E402

TOKEN_RE = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
# A token is whole only once a byte that cannot belong to it follows; a pty read can end mid-token.
TOKEN_DONE_RE = re.compile(rb"sk-ant-oat01-[A-Za-z0-9_\-]{20,}(?=[^A-Za-z0-9_\-])")
URL_RE = re.compile(rb"https://claude\.(?:com|ai)/[^\s\x07\x1b\"'<>]*oauth/authorize\?[^\s\x07\x1b\"'<>]+")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]")
MINT_TIMEOUT_S = 15 * 60
SETTLE_S = 3.0          # after the token's first bytes, how long to wait for the rest
KEEPER_DIR = "$HOME/.claude/accounts/meter-tokens"   # expanded by the shell that receives it
REMOTE_READER = '"$HOME/.local/bin/claude-account"'
REMOTE_PATH = 'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"'
EXTRA_PATH = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
HOST_RE = re.compile(r"^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# setup-token takes no --email, but it builds the same authorize URL `claude auth login --email`
# does, which carries the email as `login_hint`. This opener adds it on the way to the browser.
# GLIDESLOPE_BROWSER names another command to receive the URL (an agent driving its own browser).
OPENER = """#!/bin/sh
url="$1"
case "$url" in
  https://claude.com/cai/oauth/authorize\\?*|https://claude.ai/oauth/authorize\\?*)
    case "$url" in *login_hint=*) ;; *) url="$url&login_hint=$GLIDESLOPE_LOGIN_HINT" ;; esac ;;
esac
exec ${GLIDESLOPE_BROWSER:-/usr/bin/open} "$url"
"""


def say(text: str) -> None:
    print(text, flush=True)


def search_path() -> str:
    """The installers' directories first: the real Claude Code binary, never a multiplexer's wrapper."""
    parts = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
    return os.pathsep.join(EXTRA_PATH + [p for p in parts if p not in EXTRA_PATH])


def choose_keeper(requested: str | None) -> str:
    """Where tokens live: --to, else this machine if it keeps them, else the one satellite that does."""
    if requested:
        if requested != "local" and not HOST_RE.match(requested):
            raise SystemExit(f"--to must be `local` or a plain ssh host name, got: {requested}")
        return requested
    if ca.meter_token_files():
        return "local"
    try:
        import glideslope
        satellites = glideslope.read_satellites(glideslope.utc_now(), [])
        keepers = [s["host"] for s in satellites
                   if glideslope.meter_token_logins(s.get("snapshot")) is not None]
    except Exception:  # noqa: BLE001
        keepers = []
    if len(keepers) == 1:
        return keepers[0]
    raise SystemExit(("several satellites keep meter tokens (" + ", ".join(keepers) + ")" if keepers
                      else "no machine keeps meter tokens yet")
                     + "; name one: --to <ssh-host>, or --to local")


def capture_token(cmd: list[str], env: dict[str, str], cwd: str,
                  timeout: float = MINT_TIMEOUT_S) -> tuple[bytes | None, str]:
    """Run CMD in a pseudo-terminal until it prints a whole token. Returns (token, what it said).

    The returned text has every token blanked and is for diagnosis only. The authorize URL is
    printed as soon as it appears, for when no browser opened.
    """
    master, slave = pty.openpty()
    # wide enough that the token is never wrapped across lines
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=cwd, start_new_session=True)
    os.close(slave)
    out = bytearray()
    shown_url = False
    deadline = time.monotonic() + timeout
    settle: float | None = None
    try:
        while time.monotonic() < (settle or deadline):
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out.extend(chunk)
                clean = ANSI_RE.sub(b"", bytes(out))
                if not shown_url and (url := URL_RE.search(clean)):
                    say("If no browser opened, visit: " + url.group().decode(errors="replace"))
                    shown_url = True
                if TOKEN_DONE_RE.search(clean):
                    break
                if settle is None and TOKEN_RE.search(clean):
                    settle = time.monotonic() + SETTLE_S
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)
    clean = ANSI_RE.sub(b"", bytes(out))
    found = TOKEN_RE.findall(clean)
    said = TOKEN_RE.sub(b"<token>", clean).decode(errors="replace")
    return (found[-1] if found else None), said


def mint(email: str, scratch: Path) -> bytes:
    """Run `claude setup-token` in a pseudo-terminal and return the token it prints. Never echoes it."""
    claude = shutil.which("claude", path=search_path())
    if claude is None:
        raise SystemExit("Claude Code (claude) is not installed where this can find it")
    bin_dir = scratch / "bin"
    bin_dir.mkdir()
    opener = bin_dir / "open"
    opener.write_text(OPENER)
    opener.chmod(0o700)
    home = scratch / "home"  # an empty config dir: no real login home is touched
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_ACCOUNT", "CLAUDE_ACCOUNT_HOME", "CLAUDE_CODE_OAUTH_TOKEN")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{search_path()}", BROWSER=str(opener),
               CLAUDE_CONFIG_DIR=str(home), GLIDESLOPE_LOGIN_HINT=urllib.parse.quote(email),
               TERM="xterm-256color")
    say(f"opening claude.ai: approve it signed in as {email}. Waiting up to 15 minutes…")
    token, said = capture_token([claude, "setup-token"], env, str(scratch))
    if token is None:
        tail = "\n".join(line.strip() for line in said.splitlines() if line.strip())[-600:]
        raise SystemExit("setup-token ended without a token (not approved in time, or the browser "
                         "said no). Its last words:\n" + (tail or "(nothing)"))
    return token


def remote(host: str, script: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "--", host, script]


def run_reader(host: str, args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    if host == "local":
        cmd = [sys.executable, str(ROOT / "claude_account.py"), *args]
    else:
        cmd = remote(host, f"{REMOTE_PATH} {REMOTE_READER} " + " ".join(shlex.quote(a) for a in args))
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=90)


def whose_row(result: subprocess.CompletedProcess) -> dict:
    try:
        rows = json.loads(result.stdout or b"[]")
        row = rows[0] if isinstance(rows, list) and rows else rows
    except ValueError:
        row = {}
    row = dict(row) if isinstance(row, dict) else {}
    row["exit"] = result.returncode
    return row


def verify_here(token: bytes, login: str, scratch: Path) -> dict:
    """Which account does this token bill? `whose` exit: 0 LOGIN, 3 another, 4 unknown org, 2 failed.

    A failed probe (network, 5xx) is retried twice; the other answers are final.
    """
    path = scratch / "candidate.token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(token + b"\n")
    try:
        for wait in (0, 5, 15):
            time.sleep(wait)
            row = whose_row(run_reader("local", ["whose", str(path), "--expect", login, "--json"]))
            if row["exit"] != 2 or "rejected" in str(row.get("error") or ""):
                break  # an answer, or a token the API refused: asking again cannot change it
    finally:
        path.unlink(missing_ok=True)
    return row


def keeper_path(host: str, name: str) -> str:
    """The token file's path as `whose` on HOST takes it: ssh starts in the remote home."""
    if host == "local":
        return str(Path.home() / ".claude" / "accounts" / "meter-tokens" / f"{name}.token")
    return f".claude/accounts/meter-tokens/{name}.token"


def place(host: str, name: str, token: bytes) -> str:
    """Write the token on HOST as <name>.token, 0600, through stdin. Returns where, for people."""
    script = (f'umask 077 && d="{KEEPER_DIR}" && mkdir -p "$d" && chmod 700 "$d" && '
              f'cat > "$d/{name}.token.tmp" && chmod 600 "$d/{name}.token.tmp" && '
              f'mv -f "$d/{name}.token.tmp" "$d/{name}.token"')
    cmd = ["/bin/sh", "-c", script] if host == "local" else remote(host, script)
    result = subprocess.run(cmd, input=token + b"\n", capture_output=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f"could not write the token on {host}: "
                         f"{result.stderr.decode(errors='replace').strip()[:200]}")
    return f"~/.claude/accounts/meter-tokens/{name}.token"


def to_bitwarden(token: bytes, display: str, email: str, host: str, target: str) -> str:
    """One vault item per account: replaced in place when it exists, created when it does not."""
    bw = shutil.which("bw", path=search_path())
    if bw is None:
        return "Bitwarden copy skipped: the bw CLI is not installed"
    try:
        state = json.loads(subprocess.run([bw, "status"], capture_output=True, timeout=30).stdout or b"{}")
    except (ValueError, OSError, subprocess.SubprocessError):
        state = {}
    if state.get("status") != "unlocked":
        return ("Bitwarden copy skipped: the vault is locked, so the keeper holds the only copy. "
                f"Unlock it (export BW_SESSION) and run: tools/add-meter-token --backup {display.lower()}")
    name = f"Glideslope meter token · {display}"
    notes = (f"claude setup-token for {email}, read-only use by Glideslope's meter probe. "
             f"Placed on {host} at {target} on {dt.date.today().isoformat()}.")
    try:
        listed = json.loads(subprocess.run([bw, "list", "items", "--search", name],
                                           capture_output=True, timeout=60).stdout or b"[]")
    except (ValueError, OSError, subprocess.SubprocessError):
        listed = []
    existing = [item for item in listed if isinstance(item, dict) and item.get("name") == name]
    if existing:
        item = existing[0]
        item["notes"] = notes
        item["login"] = dict(item.get("login") or {}, username=email, password=token.decode())
        cmd, verb = [bw, "edit", "item", item["id"]], "updated"
    else:
        item = {"type": 1, "name": name, "notes": notes,
                "login": {"username": email, "password": token.decode()}}
        cmd, verb = [bw, "create", "item"], "saved"
    result = subprocess.run(cmd, input=base64.b64encode(json.dumps(item).encode()),
                            capture_output=True, timeout=60)
    if result.returncode != 0:
        return "Bitwarden copy failed: " + result.stderr.decode(errors="replace").strip()[:160]
    subprocess.run([bw, "sync"], capture_output=True, timeout=60)
    return f"Bitwarden: {verb} '{name}'"


def backup(host: str, name: str, login: str, display: str, email: str) -> int:
    """Copy a keeper's token into Bitwarden, once the keeper confirms which account it bills."""
    check = whose_row(run_reader(host, ["whose", keeper_path(host, name), "--expect", login, "--json"]))
    if check["exit"] != 0:
        raise SystemExit(f"{host}'s {name}.token does not read as {display} (whose exit {check['exit']}); "
                         "not backed up")
    script = f'cat "{KEEPER_DIR}/{name}.token"'
    cmd = ["/bin/sh", "-c", script] if host == "local" else remote(host, script)
    found = TOKEN_RE.findall(subprocess.run(cmd, capture_output=True, timeout=60).stdout or b"")
    if not found:
        raise SystemExit(f"no token in {host}'s {name}.token")
    say(to_bitwarden(found[-1], display, email, host, f"~/.claude/accounts/meter-tokens/{name}.token"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("account", help="call-sign, roster alias or email")
    parser.add_argument("--to", default=None, help="ssh host that keeps meter tokens, or `local` "
                        "(default: this machine if it keeps them, else the satellite whose beacon does)")
    parser.add_argument("--backup", action="store_true",
                        help="only copy the keeper's existing token into Bitwarden")
    args = parser.parse_args()

    ca.CALL_SIGNS.update(ca.call_signs_by_login())
    roster = ca.roster_read()
    try:
        login = ca.resolve_exact_login(args.account, roster)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    email, org = ca.split_login(login)
    display = ca.call_sign_for(login) or args.account
    name = display.lower().replace("@", "-at-").replace("#", "-")
    if not NAME_RE.match(name):
        raise SystemExit(f"cannot make a token file name from '{display}'")
    host = choose_keeper(args.to)
    if args.backup:
        return backup(host, name, login, display, email)

    with tempfile.TemporaryDirectory(prefix="meter-token-") as tmp:
        scratch = Path(tmp)
        os.chmod(scratch, 0o700)
        token = mint(email, scratch)
        check = verify_here(token, login, scratch)
        if check["exit"] == 2:
            raise SystemExit("could not verify the token (the probe failed three times: "
                             f"{check.get('error') or 'no answer'}), so it was not placed. Run this again.")
        if check["exit"] == 3:
            billed = check.get("call_sign") or check.get("email") or "another account"
            raise SystemExit(f"that token bills {billed}, not {display}, so it was not placed. "
                             f"Sign claude.ai in as {email} and run this again.")
        if check["exit"] != 0:
            raise SystemExit(f"that token bills an organization this roster does not know "
                             f"(whose exit {check['exit']}), so it was not placed. "
                             f"Sign claude.ai in as {email} and run this again.")
        say(f"verified: the token bills {display} ({email})")
        target = place(host, name, token)
        say(f"placed on {host}: {target} (0600)")
        if host != "local":
            # No --alias: the keeper may know this account under its own alias already, and a
            # second entry with the same organization would make both ambiguous.
            noted = run_reader(host, ["note", email, "--org", org])
            if noted.returncode != 0:
                say(f"{host}'s roster did not take {display}: "
                    + noted.stderr.decode(errors="replace").strip()[:160])
        there = whose_row(run_reader(host, ["whose", keeper_path(host, name), "--expect", login, "--json"]))
        say(f"{host} reads it: " + (f"yes, it bills {display}" if there["exit"] == 0
                                     else f"not yet (whose exit {there['exit']}); its next beacon retries"))
        say(to_bitwarden(token, display, email, host, target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
