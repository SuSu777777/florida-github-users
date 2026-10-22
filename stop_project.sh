#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LABEL=com.tay.florida-github-users
DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL"
  echo 'Stopped local server'
fi
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
python3 - "$PROJECT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import gui
gui.set_collection_job(500, False)
PY
