#!/usr/bin/env bash
#
# Refresh Glideslope end-to-end: gauge → the host's popup preview + Detail view.
#
# Safe to run at any time, from anywhere, concurrently. `claude-account` borrows
# the access token Claude Code already keeps and never refreshes it, so the gauge
# step is a pure reader with nothing to serialize. The single-writer lock this
# script used to take — and the OAuth landmine it guarded — were retired
# 2026-08-06 along with the refresh-token store.
#
# Usage:
#   tools/refresh.sh              gauge + both views   (the full thing)
#   tools/refresh.sh --no-gauge   views only           (skip the usage GET)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GAUGE=1
for a in "$@"; do
  case "$a" in
    --no-gauge) GAUGE=0 ;;
    -h|--help)  sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

if [ "$GAUGE" = 1 ]; then
  step "gauge — reading /api/oauth/usage (read-only; nothing is minted)"
  TMP="$(mktemp)"
  SNAPSHOT="$(python3 -c 'import glideslope; print(glideslope.DEFAULT_CLAUDE_SNAPSHOT)')"
  mkdir -p "$(dirname "$SNAPSHOT")"
  if python3 claude_account.py status --json > "$TMP" 2>"$TMP.err"; then
    # Only clobber a known-good snapshot if the new one actually parses. A truncated or
    # error-body snapshot would poison every window and reset time on the page.
    if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d and all('limits' in v for v in d.values())" "$TMP" 2>/dev/null; then
      mv "$TMP" "$SNAPSHOT"
      echo "  ✓ gauge refreshed"
    else
      echo "  ✗ snapshot did not parse — keeping the previous one" >&2
      head -c 300 "$TMP" >&2; echo >&2; rm -f "$TMP"
    fi
  else
    echo "  ✗ claude-account failed — keeping the previous gauge" >&2
    cat "$TMP.err" >&2; rm -f "$TMP"
  fi
  rm -f "$TMP.err"
else
  step "gauge — skipped (--no-gauge); percents and reset times stay as they were"
fi

step "views — rebuilding the host's popup preview + Detail view from live position + sample store"
python3 views/deck-src/build.py 2>&1 | sed 's/^/  /'

python3 - <<'PY'
import datetime as dt, os, glideslope
g = dt.datetime.fromtimestamp(os.stat(glideslope.DEFAULT_CLAUDE_SNAPSHOT).st_mtime, dt.timezone.utc)
age = (dt.datetime.now(dt.timezone.utc) - g).total_seconds() / 60
print(f"\n\033[1mgauge is now {age:.0f}m old\033[0m", end="  ")
print("— the 5h window can be read" if age < 300 else
      "\033[33m— still older than a 5h window, so the 5h read is marked unknown\033[0m")
PY

echo
echo "Detail view: open views/deck.html"
