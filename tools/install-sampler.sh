#!/usr/bin/env bash
#
# Install (or reinstall) the Glideslope sampler as a launchd agent.
#
#   tools/install-sampler.sh            install + start, then run one sample now
#   tools/install-sampler.sh --remove   stop + uninstall
#
# Cadence: every 60s (raised from 300s 2026-09-12: a switch should show up
# within a minute, and one run costs ~11s). Token discipline lives entirely inside glideslope.py
# (single-writer lock; access tokens reused to expiry) — see sampler.py header.
# On a laptop that sleeps, gaps are expected and fine; run it on an always-on
# machine for continuous coverage when that matters.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.stage11.glideslope.sampler"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$(python3 "$ROOT/glideslope.py" --print-store)"
PY="$(command -v python3)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ sampler removed"
  exit 0
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$ROOT/sampler.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- launchd's bare PATH loses codex (app-server) and homebrew python;
         mirror an interactive shell's resolution order -->
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/sampler.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/sampler.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart "gui/$(id -u)/$LABEL"
echo "✓ sampler installed ($LABEL, every 60s) — log: $LOG_DIR/sampler.log"
