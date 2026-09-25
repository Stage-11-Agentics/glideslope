#!/usr/bin/env bash
#
# Install (or reinstall) the Kimi resume poller as a launchd agent.
#
#   tools/install-kimi-resume.sh surface:200   install + start, targeting that pane
#   tools/install-kimi-resume.sh --remove      stop + uninstall
#   tools/install-kimi-resume.sh --status      what it would do right now
#
# Cadence: every 300s. Every tick re-reads Kimi's own position and the session log,
# so the poller has no interval arithmetic to drift and no reset phase to track —
# see tools/kimi_resume.py for the four gates. It is deliberately per-pane: one
# Kimi session at a time is the working assumption, and re-running with a different
# surface ref retargets it.
#
# On a laptop that sleeps, gaps are expected. A missed tick
# only delays a nudge by one interval; nothing accumulates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.stage11.glideslope.kimi-resume"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$(python3 "$ROOT/glideslope.py" --print-store)"
PY="$(command -v python3)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "✓ kimi-resume removed"
  exit 0
fi

if [ "${1:-}" = "--status" ]; then
  SURFACE="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:3' "$PLIST" 2>/dev/null || true)"
  if [ -z "$SURFACE" ]; then echo "not installed"; exit 1; fi
  echo "target: $SURFACE"
  exec "$PY" "$ROOT/tools/kimi_resume.py" --surface "$SURFACE" --status
fi

SURFACE="${1:-}"
if [ -z "$SURFACE" ]; then
  echo "usage: $(basename "$0") <surface-ref>   e.g. surface:200" >&2
  exit 2
fi
case "$SURFACE" in
  surface:*) ;;
  *) echo "✗ expected a surface ref like 'surface:200', got '$SURFACE'" >&2; exit 2 ;;
esac

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
    <string>$ROOT/tools/kimi_resume.py</string>
    <string>--surface</string>
    <string>$SURFACE</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/kimi-resume.stdout.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/kimi-resume.stderr.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart "gui/$(id -u)/$LABEL"
echo "✓ kimi-resume installed ($LABEL, every 300s) → $SURFACE"
echo "  decisions: $LOG_DIR/kimi-resume.log"
