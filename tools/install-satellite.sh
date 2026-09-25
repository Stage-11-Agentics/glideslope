#!/usr/bin/env bash
#
# Install the Glideslope satellite beacon on ANOTHER machine, over ssh.
#
#   tools/install-satellite.sh studio studio            install + start + verify
#   tools/install-satellite.sh studio studio --remove   stop + uninstall
#
# <ssh-host> is a plain ssh host name or alias (letters, digits, dots, dashes,
# underscores, optionally user@), never an option string. Keep ports and keys
# in ~/.ssh/config under the alias.
#
# Why a beacon and not an SSH pull: macOS will not release a keychain secret to
# an SSH session (errSecInteractionNotAllowed, exit 36), so the gauge can only be
# taken on the satellite itself, inside its GUI session. A LaunchAgent bootstrapped
# into gui/<uid> is exactly that session, and it reads the keychain without any
# prompt once Claude Code has been logged in there (verified on a real satellite).
#
# What lands on the remote: claude-account (the read-only gauge reader) and
# satellite_beacon.py, both under ~/.local/bin, plus one LaunchAgent. No
# credential is copied in either direction; the beacon reads the token the remote
# already holds and publishes numbers only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${1:?usage: install-satellite.sh <ssh-host> [satellite-name] [--remove]}"
NAME="${2:-$HOST}"
if [[ ! "$HOST" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "install-satellite.sh: <ssh-host> must be a plain host name or alias, got: $HOST" >&2
  exit 2
fi
LABEL="ai.stage11.glideslope.satellite"
READER="$HOME/.local/bin/claude-account"

ssh_do() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" "$@"; }

REMOTE_HOME="$(ssh_do 'printf %s "$HOME"')"
REMOTE_UID="$(ssh_do 'id -u')"
PLIST="$REMOTE_HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${3:-${2:-}}" = "--remove" ]; then
  ssh_do "launchctl bootout gui/$REMOTE_UID '$PLIST' 2>/dev/null || true; rm -f '$PLIST'"
  echo "✓ $NAME: beacon removed"
  exit 0
fi

[ -x "$READER" ] || { echo "✗ $READER not found — install claude-account here first" >&2; exit 1; }

ssh_do "mkdir -p '$REMOTE_HOME/.local/bin' '$REMOTE_HOME/Library/LaunchAgents' '$REMOTE_HOME/.glideslope'"
scp -q "$READER" "$HOST:$REMOTE_HOME/.local/bin/claude-account"
scp -q "$ROOT/tools/satellite_beacon.py" "$HOST:$REMOTE_HOME/.local/bin/glideslope-beacon"
ssh_do "chmod 755 '$REMOTE_HOME/.local/bin/claude-account' '$REMOTE_HOME/.local/bin/glideslope-beacon'"

REMOTE_PY="$(ssh_do 'command -v python3 || echo /usr/bin/python3')"

# The plist is generated here and written there in one motion, so there is one
# source for it and no copy to drift. Same rule as every other deployed config
# in this repo: deploying IS syncing.
ssh_do "cat > '$PLIST'" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REMOTE_PY</string>
    <string>$REMOTE_HOME/.local/bin/glideslope-beacon</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$REMOTE_HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>GLIDESLOPE_SATELLITE</key><string>$NAME</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$REMOTE_HOME/.glideslope/satellite.log</string>
  <key>StandardErrorPath</key><string>$REMOTE_HOME/.glideslope/satellite.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

ssh_do "launchctl bootout gui/$REMOTE_UID '$PLIST' 2>/dev/null || true
        launchctl bootstrap gui/$REMOTE_UID '$PLIST'
        launchctl kickstart -k gui/$REMOTE_UID/$LABEL"

# Verify the real artifact, not the install: a beacon that exists and parses.
sleep 4
if ssh_do "python3 -c \"import json,sys;d=json.load(open('$REMOTE_HOME/.glideslope/satellite.json'));sys.exit(0 if d.get('snapshot') else 1)\"" 2>/dev/null; then
  echo "✓ $NAME: beacon live (every 300s) — $REMOTE_HOME/.glideslope/satellite.json"
else
  echo "⚠ $NAME: agent installed but the beacon has no gauge yet — check $REMOTE_HOME/.glideslope/satellite.log"
  ssh_do "tail -5 '$REMOTE_HOME/.glideslope/satellite.log' 2>/dev/null" || true
fi
