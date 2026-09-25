#!/usr/bin/env bash
#
# Install (or reinstall) the Glideslope login watcher as a launchd agent.
#
#   tools/install-login-watch.sh            install + start
#   tools/install-login-watch.sh --remove   stop + uninstall
#
# Fires tools/login_watch.py whenever a Claude login file changes, which restarts
# the sampler only if the logged-in accounts or the selected one actually changed.
# WatchPaths cannot glob: re-run this after creating a new login home
# (claude-account login <account>). Until then the 60s sampler still catches it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.stage11.glideslope.login-watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$(python3 "$ROOT/glideslope.py" --print-store)"
PY="$(command -v python3)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ login watcher removed"
  exit 0
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents" "$HOME/.claude/accounts"

WATCH="    <string>$HOME/.claude.json</string>
    <string>$HOME/.claude/accounts/selection.json</string>"
if [ -d "$HOME/.claude-profiles" ]; then
  WATCH="$WATCH
    <string>$HOME/.claude-profiles</string>"
  for home in "$HOME"/.claude-profiles/*/; do
    [ -d "$home" ] || continue
    WATCH="$WATCH
    <string>${home%/}/.claude.json</string>"
  done
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/tools/login_watch.py</string>
  </array>
  <key>WatchPaths</key>
  <array>
$WATCH
  </array>
  <key>ThrottleInterval</key><integer>3</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/login-watch.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/login-watch.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "✓ login watcher installed ($LABEL) — log: $LOG_DIR/login-watch.log"
