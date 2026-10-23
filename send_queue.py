#!/usr/bin/env python3
"""Send one opted-in email per launch, respecting a rolling daily cap."""
import fcntl
import smtplib
import sys
from datetime import datetime, timezone
from pathlib import Path

import github_florida as app

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "github_users.sqlite3"
LOCK_PATH = ROOT / ".email_queue.lock"
MIN_INTERVAL_SECONDS = 120


def queue_status(conn):
    return conn.execute("SELECT active, consecutive_errors FROM email_queue WHERE id=1").fetchone()


def update_queue(conn, *, active=None, error=None, errors=None, finished=False):
    updates = ["last_error=?"]
    values = [error]
    if active is not None:
        updates.append("active=?")
        values.append(int(active))
    if errors is not None:
        updates.append("consecutive_errors=?")
        values.append(errors)
    if finished:
        updates.append("finished_at=?")
        values.append(datetime.now(timezone.utc).isoformat())
    conn.execute("UPDATE email_queue SET " + ", ".join(updates) + " WHERE id=1", values)
    conn.commit()


def last_accepted_at(conn):
    profile = conn.execute("SELECT MAX(emailed_at) FROM users").fetchone()[0]
    test = conn.execute("""SELECT MAX(recorded_at) FROM email_tests
        WHERE status='Accepted by Gmail SMTP'""").fetchone()[0]
    values = [datetime.fromisoformat(value) for value in (profile, test) if value]
    return max(values) if values else None


def run_once():
    with app.db(str(DB_PATH)) as conn:
        status = queue_status(conn)
        if not status or not status["active"]:
            return
        pending = app.eligible_recipients(conn, 1, include_failed=False)
        if not pending:
            update_queue(conn, active=False, error=None, errors=0, finished=True)
            print("Email queue finished; no unattempted eligible recipients remain", flush=True)
            return
        if app.smtp_daily_remaining(conn) == 0:
            print("Email queue waiting for the rolling 24-hour app allowance", flush=True)
            return
        previous = last_accepted_at(conn)
        if previous:
            seconds = (datetime.now(timezone.utc) - previous).total_seconds()
            if seconds < MIN_INTERVAL_SECONDS:
                print(f"Email queue waiting {MIN_INTERVAL_SECONDS - seconds:.0f}s for pacing", flush=True)
                return
        try:
            sent = app.send(conn, None, None, 1, True,
                            templates=app.load_email_templates(),
                            interval_seconds=MIN_INTERVAL_SECONDS,
                            include_failed=False)
        except smtplib.SMTPRecipientsRefused:
            update_queue(conn, error="Recipient rejected", errors=0)
            print("Recipient rejected; continuing with the next contact", flush=True)
            return
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            errors = status["consecutive_errors"] + 1
            paused = errors >= 3 or isinstance(exc, smtplib.SMTPAuthenticationError)
            update_queue(conn, active=not paused, error=type(exc).__name__, errors=errors)
            print(f"Email queue error: {type(exc).__name__}; {'paused' if paused else 'will retry'}", flush=True)
            return
        if sent:
            update_queue(conn, error=None, errors=0)


def main():
    descriptor = LOCK_PATH.open("a+")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Email queue run already active", flush=True)
        return
    run_once()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Email queue unexpected error: {type(exc).__name__}", file=sys.stderr, flush=True)
        raise
