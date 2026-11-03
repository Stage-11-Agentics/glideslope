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
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")
MINT_TIMEOUT_S = 15 * 60
KEEPER_DIR = "~/.claude/accounts/meter-tokens"
REMOTE_READER = "~/.local/bin/claude-account"
EXTRA_PATH = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]

# setup-token takes no --email, but it builds the same authorize URL `claude auth login --email`
# does, which carries the email as `login_hint`. This opener adds it on the way to the browser.
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


def default_keeper() -> str:
    """The first configured satellite: the always-on machine PROVIDERS.md says keeps the tokens."""
    try:
        import glideslope
        satellites = glideslope.REMOTE_SATELLITES
        return satellites[0]["host"] if satellites else "local"
    except Exception:  # noqa: BLE001
        return "local"


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
    master, slave = pty.openpty()
    # wide enough that the token is never wrapped across lines
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
    proc = subprocess.Popen([claude, "setup-token"], stdin=slave, stdout=slave, stderr=slave,
                            env=env, cwd=str(scratch), start_new_session=True)
    os.close(slave)
    say(f"claude.ai is open in your browser: approve it signed in as {email}. Waiting…")
    out = bytearray()
    deadline = time.monotonic() + MINT_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1.0)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out.extend(chunk)
                if TOKEN_RE.search(ANSI_RE.sub(b"", bytes(out))):
                    time.sleep(0.5)  # let it finish printing, then stop it
                    break
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
    found = TOKEN_RE.findall(ANSI_RE.sub(b"", bytes(out)))
    out[:] = b"\0" * len(out)
    if not found:
        raise SystemExit("setup-token ended without a token (not approved within 15 minutes, "
                         "or the browser said no)")
    return found[-1]


def run_reader(host: str, args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    if host == "local":
        cmd = [sys.executable, str(ROOT / "claude_account.py"), *args]
    else:
        remote = f"PATH={shlex.quote(':'.join(EXTRA_PATH))}:$PATH {REMOTE_READER} " + " ".join(
            shlex.quote(a) for a in args)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, remote]
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=60)


def verify_here(token: bytes, login: str, scratch: Path) -> dict:
    """One probe: which account does this token bill? Exit 0 only for LOGIN."""
    path = scratch / "candidate.token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(token + b"\n")
    try:
        result = run_reader("local", ["whose", str(path), "--expect", login, "--json"])
    finally:
        path.unlink(missing_ok=True)
    try:
        rows = json.loads(result.stdout or b"[]")
        row = rows[0] if isinstance(rows, list) and rows else rows
    except ValueError:
        row = {}
    row = row if isinstance(row, dict) else {}
    row["exit"] = result.returncode
    return row


def place(host: str, name: str, token: bytes) -> str:
    """Write the token on HOST as <name>.token, 0600, through stdin. Returns its path there."""
    target = f"{KEEPER_DIR}/{name}.token"
    script = (f"umask 077 && mkdir -p {KEEPER_DIR} && chmod 700 {KEEPER_DIR} && "
              f"cat > {target}.tmp && chmod 600 {target}.tmp && mv -f {target}.tmp {target}")
    if host == "local":
        cmd = ["/bin/sh", "-c", script.replace("~", str(Path.home()))]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, script]
    result = subprocess.run(cmd, input=token + b"\n", capture_output=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f"could not write the token on {host}: "
                         f"{result.stderr.decode(errors='replace').strip()[:200]}")
    return target


def to_bitwarden(token: bytes, display: str, email: str, host: str, target: str) -> str:
    bw = shutil.which("bw", path=search_path())
    if bw is None:
        return "Bitwarden copy skipped: the bw CLI is not installed"
    try:
        state = json.loads(subprocess.run([bw, "status"], capture_output=True, timeout=30).stdout or b"{}")
    except (ValueError, OSError, subprocess.SubprocessError):
        state = {}
    if state.get("status") != "unlocked":
        return ("Bitwarden copy skipped: the vault is locked. The keeper holds the only copy; "
                f"unlock (export BW_SESSION) and run: tools/add-meter-token --backup {display.lower()}")
    item = {
        "type": 1, "name": f"Glideslope meter token · {display}",
        "notes": (f"claude setup-token for {email}, read-only use by Glideslope's meter probe. "
                  f"Placed on {host} at {target} on {dt.date.today().isoformat()}."),
        "login": {"username": email, "password": token.decode()},
    }
    encoded = base64.b64encode(json.dumps(item).encode())
    result = subprocess.run([bw, "create", "item"], input=encoded, capture_output=True, timeout=60)
    if result.returncode != 0:
        return "Bitwarden copy failed: " + result.stderr.decode(errors="replace").strip()[:160]
    subprocess.run([bw, "sync"], capture_output=True, timeout=60)
    return f"Bitwarden: saved as 'Glideslope meter token · {display}'"


def backup(host: str, name: str, display: str, email: str) -> int:
    """Copy a token already on the keeper into Bitwarden."""
    target = f"{KEEPER_DIR}/{name}.token"
    if host == "local":
        cmd = ["/bin/cat", target.replace("~", str(Path.home()))]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"cat {target}"]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    found = TOKEN_RE.findall(result.stdout or b"")
    if not found:
        raise SystemExit(f"no token at {host}:{target}")
    say(to_bitwarden(found[-1], display, email, host, target))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("account", help="call-sign, roster alias or email")
    parser.add_argument("--to", default=None, help="ssh host that keeps meter tokens, or `local` "
                        "(default: the first [[satellites]] host in Glideslope's config)")
    parser.add_argument("--backup", action="store_true",
                        help="only copy the keeper's existing token into Bitwarden")
    args = parser.parse_args()

    ca.CALL_SIGNS.update(ca.call_signs_by_login())
    roster = ca.roster_read()
    login = ca.resolve_login(args.account, roster)
    email, org = ca.split_login(login)
    if not login or not email:
        raise SystemExit(f"unknown account '{args.account}': name a call-sign, roster alias or email")
    if not org:
        raise SystemExit(f"{args.account}'s organization is not known here: sign it in once "
                         "(claude-account login), or claude-account note <email> --org <id>")
    display = ca.call_sign_for(login) or args.account
    name = display.lower().replace("@", "-at-").replace("#", "-")
    host = args.to or default_keeper()
    if args.backup:
        return backup(host, name, display, email)

    with tempfile.TemporaryDirectory(prefix="meter-token-") as tmp:
        scratch = Path(tmp)
        os.chmod(scratch, 0o700)
        token = mint(email, scratch)
        check = verify_here(token, login, scratch)
        if check["exit"] != 0:
            billed = check.get("call_sign") or check.get("email") or "an account this roster lacks"
            raise SystemExit(f"that token bills {billed}, not {display}, so it was not placed. "
                             f"Sign claude.ai in as {email} and run this again.")
        say(f"verified: the token bills {display} ({email})")
        target = place(host, name, token)
        say(f"placed on {host}: {target} (0600)")
        if host != "local":
            noted = run_reader(host, ["note", email, "--org", org, "--alias", name])
            if noted.returncode != 0 and b"already" not in noted.stderr:
                say(f"{host}'s roster did not take {display}: "
                    + noted.stderr.decode(errors="replace").strip()[:160])
        # ssh starts in the remote home, so a home-relative path needs no tilde (which quoting would keep literal)
        where = target.replace("~", str(Path.home())) if host == "local" else target.removeprefix("~/")
        there = run_reader(host, ["whose", where, "--expect", login, "--json"])
        say(f"{host} reads it: " + ("yes, it bills " + display if there.returncode == 0
                                     else f"not yet (exit {there.returncode}); its next beacon retries"))
        say(to_bitwarden(token, display, email, host, target))
        token = b"\0" * len(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
