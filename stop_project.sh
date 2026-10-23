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
import github_florida as app
from pathlib import Path
with app.db(str(Path(sys.argv[1]) / 'github_users.sqlite3')) as conn:
    conn.execute('UPDATE collection_job SET active=0 WHERE id=1')
    conn.commit()
PY
