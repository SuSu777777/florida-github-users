#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LABEL=com.tay.florida-github-mailer
DOMAIN="gui/$(id -u)"
cd "$PROJECT_DIR"
python3 - <<'PY'
import github_florida as app
with app.db('github_users.sqlite3') as conn:
    conn.execute('UPDATE email_queue SET active=0 WHERE id=1')
    conn.commit()
PY
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL"
fi
echo "Email queue stopped."
