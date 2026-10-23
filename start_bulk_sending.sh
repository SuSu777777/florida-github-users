#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LABEL=com.tay.florida-github-mailer
AGENT_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
cd "$PROJECT_DIR"
mkdir -p logs
python3 - "$PROJECT_DIR" "$AGENT_FILE" "$LABEL" <<'PY'
import plistlib
import sys
from datetime import datetime, timezone
from pathlib import Path
import github_florida as app

project, agent_file, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
with app.db(str(project / 'github_users.sqlite3')) as conn:
    pending = len(app.eligible_recipients(conn, 500, include_failed=False))
    if not pending:
        raise SystemExit('No opted-in, unattempted email recipients')
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT INTO email_queue
        (id, active, started_at, finished_at, last_error, consecutive_errors)
        VALUES (1, 1, ?, NULL, NULL, 0)
        ON CONFLICT(id) DO UPDATE SET active=1, started_at=excluded.started_at,
        finished_at=NULL, last_error=NULL, consecutive_errors=0""", (now,))
    conn.commit()
agent_file.parent.mkdir(parents=True, exist_ok=True)
agent_file.write_bytes(plistlib.dumps({
    'Label': label,
    'ProgramArguments': [sys.executable, '-u', str(project / 'send_queue.py')],
    'WorkingDirectory': str(project),
    'RunAtLoad': True,
    'StartInterval': 120,
    'StandardOutPath': str(project / 'logs/bulk_email.log'),
    'StandardErrorPath': str(project / 'logs/bulk_email.log'),
}))
PY
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "$DOMAIN/$LABEL"
else
  launchctl bootstrap "$DOMAIN" "$AGENT_FILE"
fi
echo "Email queue started; one message per 120 seconds, within the app daily allowance."
