#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LABEL=com.tay.florida-github-users
AGENT_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
cd "$PROJECT_DIR"
mkdir -p logs
python3 - "$PROJECT_DIR" "$AGENT_FILE" "$LABEL" <<'PY'
import plistlib
import sys
from pathlib import Path
project, agent_file, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
agent_file.parent.mkdir(parents=True, exist_ok=True)
agent_file.write_bytes(plistlib.dumps({
    'Label': label,
    'ProgramArguments': [sys.executable, '-u', str(project / 'gui.py')],
    'WorkingDirectory': str(project),
    'RunAtLoad': True,
    'KeepAlive': True,
    'StandardOutPath': str(project / 'logs/project.log'),
    'StandardErrorPath': str(project / 'logs/project.log'),
}))
PY
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  if curl -fsS --max-time 2 http://127.0.0.1:8765/status >/dev/null 2>&1; then
    echo 'Already running at http://127.0.0.1:8765'
    exit 0
  fi
  launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null
else
  launchctl bootstrap "$DOMAIN" "$AGENT_FILE"
fi
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 1 http://127.0.0.1:8765/status >/dev/null 2>&1; then
    echo 'Running at http://127.0.0.1:8765'
    exit 0
  fi
  sleep 1
done
echo "Startup failed; see $PROJECT_DIR/logs/project.log" >&2
exit 1
