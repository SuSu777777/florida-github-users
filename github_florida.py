#!/usr/bin/env python3
"""Collect public GitHub profiles by US state and email opted-in contacts."""
import argparse
import base64
import csv
import json
import os
import re
import secrets
import sqlite3
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

API = "https://api.github.com"
US_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
)
REQUEST_INTERVAL_SECONDS = 1.0
_request_lock = threading.Lock()
_last_request_finished = None
EXPORT_COLUMNS = ("github_id", "login", "name", "location", "state", "email",
                  "email_source", "phone", "phone_source", "profile_url", "website", "social_accounts",
                  "fetched_at", "opted_in", "emailed_at", "sent_template",
                  "email_status", "email_status_at", "email_error")


def smtp_credentials():
    """Read SMTP settings from the process environment or the local .env file."""
    settings = {}
    env_path = Path(__file__).with_name(".env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in ("SMTP_USER", "SMTP_APP_PASSWORD"):
                settings[key.strip()] = value.strip().strip('"').strip("'")
    return (os.getenv("SMTP_USER") or settings.get("SMTP_USER", ""),
            os.getenv("SMTP_APP_PASSWORD") or settings.get("SMTP_APP_PASSWORD", ""))


def default_github_token():
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token
    try:
        from local_config import GITHUB_TOKEN
        return GITHUB_TOKEN
    except ImportError:
        return ""


def db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        github_id INTEGER PRIMARY KEY, login TEXT NOT NULL, name TEXT,
        location TEXT, state TEXT NOT NULL DEFAULT 'Florida', email TEXT,
        profile_url TEXT NOT NULL,
        fetched_at TEXT NOT NULL, opted_in INTEGER NOT NULL DEFAULT 0,
        emailed_at TEXT
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "state" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN state TEXT NOT NULL DEFAULT 'Florida'")
    for name in ("email_source", "website", "social_accounts", "sent_template",
                 "phone", "phone_source", "phone_checked_at", "email_status",
                 "email_status_at", "email_error"):
        if name not in columns:
            kind = "INTEGER" if name == "sent_template" else "TEXT"
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {kind}")
    conn.execute("""CREATE TABLE IF NOT EXISTS collection_job (
        id INTEGER PRIMARY KEY CHECK (id = 1), target INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'Florida'
    )""")
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(collection_job)")}
    if "state" not in job_columns:
        conn.execute("ALTER TABLE collection_job ADD COLUMN state TEXT NOT NULL DEFAULT 'Florida'")
    conn.execute("""CREATE TABLE IF NOT EXISTS collection_cursor (
        state TEXT PRIMARY KEY, created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_tests (
        id INTEGER PRIMARY KEY, recipient TEXT NOT NULL, status TEXT NOT NULL,
        template_number INTEGER, recorded_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_queue (
        id INTEGER PRIMARY KEY CHECK (id = 1), active INTEGER NOT NULL DEFAULT 0,
        started_at TEXT, finished_at TEXT, last_error TEXT,
        consecutive_errors INTEGER NOT NULL DEFAULT 0
    )""")
    conn.commit()
    return conn


def get_json(path, token, progress=None):
    global _last_request_finished
    for attempt in range(4):
        if progress:
            progress(f"GitHub request: {path} (attempt {attempt + 1}/4)")
        try:
            with _request_lock:
                if _last_request_finished is not None:
                    wait = REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_finished)
                    if wait > 0:
                        time.sleep(wait)
                try:
                    with tempfile.TemporaryDirectory() as folder:
                        headers = Path(folder) / "headers"
                        body = Path(folder) / "body"
                        command = ["curl", "--config", "-", "--ipv4", "--silent",
                                   "--show-error", "--connect-timeout", "5", "--max-time", "12",
                                   "--dump-header", str(headers), "--output", str(body),
                                   "--write-out", "%{http_code}",
                                   "--header", "Accept: application/vnd.github+json",
                                   "--header", "X-GitHub-Api-Version: 2022-11-28",
                                   "--header", "User-Agent: us-state-github-contacts/1.0",
                                   API + path]
                        config = ('header = "Authorization: Bearer ' + token + '"\n') if token else ""
                        result = subprocess.run(command, input=config, text=True,
                                                capture_output=True, timeout=15)
                        status = int(result.stdout.strip() or "0")
                        response_headers = {}
                        if headers.exists():
                            for line in headers.read_text(errors="replace").splitlines():
                                if ":" in line:
                                    key, value = line.split(":", 1)
                                    response_headers[key.lower()] = value.strip()
                        response_body = body.read_text(errors="replace") if body.exists() else ""
                finally:
                    _last_request_finished = time.monotonic()
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"curl exited {result.returncode}")
            if status in (403, 429) and attempt < 3:
                reset = int(response_headers.get("x-ratelimit-reset", "0"))
                retry = int(response_headers.get("retry-after", "0"))
                delay = max(retry, reset - int(time.time()) + 1)
                if delay <= 0:
                    delay = 60
                if delay > 120:
                    raise RuntimeError("GitHub rate limit reached; retry after its reset time")
                time.sleep(delay)
                continue
            if status < 200 or status >= 300:
                try:
                    detail = json.loads(response_body).get("message", "")
                except ValueError:
                    detail = response_body[:150]
                raise RuntimeError(f"GitHub API returned HTTP {status}: {detail}")
            return json.loads(response_body)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub network error after retries: {exc}") from exc
        except RuntimeError as exc:
            if str(exc).startswith("GitHub API returned") or str(exc).startswith("GitHub rate limit"):
                raise
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub network error after retries: {exc}") from exc


def get_social_accounts(login, token, progress=None):
    accounts = []
    for page in range(1, 11):
        batch = get_json("/users/" + urllib.parse.quote(login) +
                         f"/social_accounts?per_page=100&page={page}", token, progress)
        accounts.extend(entry for entry in batch if isinstance(entry, dict) and entry.get("url"))
        if len(batch) < 100:
            break
    return accounts


PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?1[\s.()-]*)?(?:\([2-9]\d{2}\)|[2-9]\d{2})[\s.()-]*[2-9]\d{2}[\s.()-]*\d{4}(?!\d)")
PHONE_LABEL = re.compile(r"\b(?:phone|mobile|cell|telephone|tel|call(?: me)?)\s*[:：-]", re.I)


def public_phone(text):
    """Extract a US phone number only when its owner labels it as contact info."""
    for line in (text or "").splitlines():
        label = PHONE_LABEL.search(line)
        if not label:
            continue
        match = PHONE_PATTERN.search(line[label.end():])
        if match:
            return match.group().strip()
    return None


def github_phone(profile, token, progress=None):
    phone = public_phone(profile.get("bio"))
    if phone:
        return phone, profile["html_url"]
    login = profile["login"]
    path = "/repos/" + urllib.parse.quote(login) + "/" + urllib.parse.quote(login) + "/readme"
    try:
        readme = get_json(path, token, progress)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None, None
        raise
    if readme.get("encoding") != "base64":
        return None, None
    content = base64.b64decode(readme.get("content", ""), validate=False).decode("utf-8", "replace")
    phone = public_phone(content)
    return (phone, readme.get("html_url")) if phone else (None, None)


def collect(conn, count, token, progress=None, state="Florida"):
    if not 1 <= count <= 500:
        raise ValueError("count must be between 1 and 500")
    if state not in US_STATES:
        raise ValueError("Choose one of the 50 US states")
    saved = 0
    skipped = 0
    cursor_row = conn.execute("SELECT created_at FROM collection_cursor WHERE state=?", (state,)).fetchone()
    cursor = cursor_row[0] if cursor_row else None
    while saved < count:
      starting_cursor = cursor
      exhausted = False
      for page in range(1, 11):
        search = f'location:"{state}" type:user'
        if starting_cursor:
            search += f' created:>={starting_cursor}'
        query = urllib.parse.urlencode({"q": search, "per_page": 100, "page": page,
                                        "sort": "joined", "order": "asc"})
        items = get_json("/search/users?" + query, token, progress).get("items", [])
        if not items:
            exhausted = True
            break
        for item in items:
            try:
                profile = get_json("/users/" + urllib.parse.quote(item["login"]), token, progress)
            except RuntimeError as exc:
                skipped += 1
                if progress:
                    progress(f"Skipped {item['login']} after GitHub request failure: {exc}")
                continue
            created_at = profile.get("created_at")
            if created_at and (cursor is None or created_at > cursor):
                cursor = created_at
                conn.execute("""INSERT INTO collection_cursor (state, created_at) VALUES (?, ?)
                    ON CONFLICT(state) DO UPDATE SET created_at=excluded.created_at""", (state, cursor))
                conn.commit()
            if conn.execute("SELECT 1 FROM users WHERE github_id=?", (item["id"],)).fetchone():
                continue
            # Search location is user-supplied; retain only explicit state-name matches.
            location = (profile.get("location") or "").strip()
            if state.casefold() not in location.casefold():
                continue
            try:
                accounts = get_social_accounts(item["login"], token, progress)
            except RuntimeError as exc:
                accounts = []
                if progress:
                    progress(f"Social account lookup failed for {item['login']}: {exc}")
            if profile.get("twitter_username"):
                accounts.append({"provider": "twitter", "url": "https://x.com/" + profile["twitter_username"]})
            phone = phone_source = phone_checked_at = None
            try:
                phone, phone_source = github_phone(profile, token, progress)
                phone_checked_at = datetime.now(timezone.utc).isoformat()
            except RuntimeError as exc:
                if progress:
                    progress(f"Phone lookup failed for {item['login']}: {exc}")
            conn.execute("""INSERT INTO users
                (github_id, login, name, location, state, email, email_source, profile_url,
                 website, social_accounts, fetched_at, phone, phone_source, phone_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(github_id) DO UPDATE SET
                login=excluded.login, name=excluded.name, location=excluded.location,
                state=excluded.state,
                email=COALESCE(excluded.email, users.email),
                email_source=COALESCE(excluded.email_source, users.email_source),
                profile_url=excluded.profile_url, website=excluded.website,
                social_accounts=excluded.social_accounts,
                phone=COALESCE(excluded.phone, users.phone),
                phone_source=COALESCE(excluded.phone_source, users.phone_source),
                phone_checked_at=COALESCE(excluded.phone_checked_at, users.phone_checked_at),
                fetched_at=excluded.fetched_at""",
                (profile["id"], profile["login"], profile.get("name"), location, state,
                 profile.get("email"), "github_profile" if profile.get("email") else None,
                 profile["html_url"], profile.get("blog"), json.dumps(accounts, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(), phone, phone_source, phone_checked_at))
            saved += 1
            conn.commit()
            if progress:
                progress(f"Saved {saved}/{count} {state} profiles")
            if saved % 25 == 0:
                print(f"Saved {saved}/{count}", flush=True)
            if saved >= count:
                break
        conn.commit()
        if saved >= count:
            break
        if len(items) < 100:
            exhausted = True
            break
      if exhausted or saved >= count or cursor == starting_cursor:
          break
    print(f"Saved {saved} {state} profiles; {conn.execute('SELECT COUNT(*) FROM users WHERE email IS NOT NULL').fetchone()[0]} database profiles have email addresses.")
    if skipped:
        print(f"Skipped {skipped} profiles after repeated GitHub request failures.", file=sys.stderr)
    if saved < count:
        print("Fewer new matching profiles were available in GitHub search results.", file=sys.stderr)


def enrich_phones(conn, limit, token, progress=None):
    rows = conn.execute("""SELECT github_id, login FROM users
        WHERE phone_checked_at IS NULL ORDER BY github_id LIMIT ?""", (limit,)).fetchall()
    checked = found = failed = 0
    for row in rows:
        try:
            profile = get_json("/users/" + urllib.parse.quote(row["login"]), token, progress)
            phone, source = github_phone(profile, token, progress)
        except RuntimeError as exc:
            failed += 1
            if progress:
                progress(f"Phone lookup skipped for {row['login']}: {exc}")
            continue
        conn.execute("""UPDATE users SET phone=COALESCE(?, phone),
            phone_source=COALESCE(?, phone_source), phone_checked_at=? WHERE github_id=?""",
            (phone, source, datetime.now(timezone.utc).isoformat(), row["github_id"]))
        conn.commit()
        checked += 1
        found += bool(phone)
        if progress:
            progress(f"Checked {checked}/{len(rows)} GitHub profiles; found {found} phone numbers")
    return checked, found, failed


def eligible_recipients(conn, limit=100, include_failed=True):
    return conn.execute("""SELECT github_id, login, email FROM users
        WHERE opted_in = 1 AND email IS NOT NULL AND emailed_at IS NULL
        AND (email_status IS NULL OR (? AND email_status = 'failed'))
        ORDER BY github_id LIMIT ?""", (int(include_failed), limit)).fetchall()


def smtp_daily_remaining(conn):
    """Keep this app well below the daily limit for personal Gmail accounts."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    profile_sends = conn.execute("SELECT COUNT(*) FROM users WHERE emailed_at >= ?", (cutoff,)).fetchone()[0]
    test_sends = conn.execute("""SELECT COUNT(*) FROM email_tests
        WHERE status='Accepted by Gmail SMTP' AND recorded_at >= ?""", (cutoff,)).fetchone()[0]
    return max(0, 100 - profile_sends - test_sends)


def record_email_test(conn, recipient, status, template_number=None):
    conn.execute("""INSERT INTO email_tests (recipient, status, template_number, recorded_at)
        VALUES (?, ?, ?, ?)""", (recipient, status, template_number,
                              datetime.now(timezone.utc).isoformat()))
    conn.commit()


def load_email_templates(path=None):
    path = Path(path) if path else Path(__file__).with_name("email_templates.json")
    templates = json.loads(path.read_text(encoding="utf-8"))
    if len(templates) != 6 or any("{from_email}" not in item["body"] or
                                  "{recipient_name}" not in item["body"] or
                                  "telegram" in item["body"].lower() for item in templates):
        raise ValueError("Expected six email templates with sender and recipient placeholders and no Telegram")
    return templates


def send(conn, subject, body_path, limit, live, user=None, password=None,
         progress=None, templates=None, interval_seconds=120, include_failed=True):
    if interval_seconds < 60:
        raise ValueError("SMTP send interval must be at least 60 seconds")
    default_user, default_password = smtp_credentials()
    user = user or default_user
    password = password or default_password
    if live and (not user or not password):
        raise ValueError("Set SMTP_USER and SMTP_APP_PASSWORD before sending")
    if templates is None:
        if not body_path:
            raise ValueError("message body file is required")
        body = Path(body_path).read_text(encoding="utf-8")
        if not subject or not body.strip():
            raise ValueError("subject and message body are required")
    rows = eligible_recipients(conn, min(limit, smtp_daily_remaining(conn)), include_failed)
    if not live:
        print(f"Dry run: {len(rows)} opted-in, unsent recipients. Add --send to deliver.")
        return len(rows)
    if not rows:
        print("No eligible recipients within the current app sending allowance.")
        return 0
    accepted = 0
    last_accepted_at = None
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        for row in rows:
            if last_accepted_at is not None:
                wait = max(0, interval_seconds - (time.monotonic() - last_accepted_at))
                if wait:
                    if progress:
                        progress(f"Waiting {wait:.0f}s before the next email")
                    time.sleep(wait)
            template = secrets.choice(templates) if templates else None
            msg = EmailMessage()
            msg["From"] = user
            msg["To"] = row["email"]
            msg["Subject"] = template["subject"] if template else subject
            msg.set_content(template["body"].replace("{from_email}", user)
                            .replace("{recipient_name}", row["login"]) if template else body)
            conn.execute("""UPDATE users SET email_status='unconfirmed',
                email_status_at=?, email_error=NULL WHERE github_id=?""",
                (datetime.now(timezone.utc).isoformat(), row["github_id"]))
            conn.commit()
            try:
                refused = smtp.send_message(msg)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            except smtplib.SMTPRecipientsRefused:
                conn.execute("""UPDATE users SET email_status='failed',
                    email_status_at=?, email_error='Recipient rejected' WHERE github_id=?""",
                    (datetime.now(timezone.utc).isoformat(), row["github_id"]))
                conn.commit()
                raise
            except (smtplib.SMTPException, OSError) as exc:
                conn.execute("""UPDATE users SET email_status='unconfirmed',
                    email_status_at=?, email_error=? WHERE github_id=?""",
                    (datetime.now(timezone.utc).isoformat(), type(exc).__name__, row["github_id"]))
                conn.commit()
                raise
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""UPDATE users SET emailed_at=?, sent_template=?,
                email_status='accepted', email_status_at=?, email_error=NULL
                WHERE github_id=?""",
                (now, template["number"] if template else None, now, row["github_id"]))
            conn.commit()
            accepted += 1
            last_accepted_at = time.monotonic()
            print(f"Accepted by Gmail SMTP for {row['login']}")
            if progress:
                progress(f"Accepted {accepted}/{len(rows)} emails by Gmail SMTP")
    return accepted


def export_csv(conn, path):
    def safe_cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    rows = conn.execute("SELECT " + ", ".join(EXPORT_COLUMNS) + " FROM users ORDER BY github_id")
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(EXPORT_COLUMNS)
        count = 0
        for row in rows:
            writer.writerow([safe_cell(row[column]) for column in EXPORT_COLUMNS])
            count += 1
    print(f"Exported {count} users to {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="github_users.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_cmd = commands.add_parser("collect")
    collect_cmd.add_argument("--count", type=int, default=500)
    collect_cmd.add_argument("--state", choices=US_STATES, default="Florida")
    commands.add_parser("stats")
    export_cmd = commands.add_parser("export", help="Export stored users to CSV")
    export_cmd.add_argument("--output", default="us_github_users.csv")
    consent_cmd = commands.add_parser("consent", help="Record verified recipient consent")
    consent_cmd.add_argument("login")
    consent_cmd.add_argument("--revoke", action="store_true")
    phone_cmd = commands.add_parser("find-phones", help="Check GitHub profiles and profile READMEs for public phone numbers")
    phone_cmd.add_argument("--limit", type=int, default=25)
    send_cmd = commands.add_parser("send")
    send_cmd.add_argument("--subject")
    send_cmd.add_argument("--body-file")
    send_cmd.add_argument("--templates", action="store_true", help="Choose randomly from six imported templates per recipient")
    send_cmd.add_argument("--limit", type=int, default=100)
    send_cmd.add_argument("--interval-seconds", type=int, default=120,
                          help="Pause between SMTP sends (minimum 60; default 120)")
    send_cmd.add_argument("--send", action="store_true", help="Actually deliver; default is dry run")
    args = parser.parse_args()
    try:
        conn = db(args.db)
        if args.command == "collect":
            collect(conn, args.count, default_github_token(), state=args.state)
        elif args.command == "stats":
            row = conn.execute("SELECT COUNT(*), COUNT(email), SUM(opted_in), COUNT(emailed_at) FROM users").fetchone()
            print(f"Users: {row[0]} | public emails: {row[1]} | opted in: {row[2] or 0} | emailed: {row[3]}")
        elif args.command == "export":
            export_csv(conn, args.output)
        elif args.command == "consent":
            result = conn.execute("UPDATE users SET opted_in=? WHERE login=?", (0 if args.revoke else 1, args.login))
            conn.commit()
            if not result.rowcount:
                raise ValueError("login not found in local database")
            print("Consent revoked" if args.revoke else "Consent recorded")
        elif args.command == "find-phones":
            if args.limit < 1:
                raise ValueError("limit must be positive")
            checked, found, failed = enrich_phones(conn, args.limit, default_github_token())
            print(f"Checked {checked} GitHub profiles; found {found} public phone numbers; {failed} lookup failures")
        elif args.command == "send":
            if args.limit < 1:
                raise ValueError("limit must be positive")
            send(conn, args.subject, args.body_file, args.limit, args.send,
                 templates=load_email_templates() if args.templates else None,
                 interval_seconds=args.interval_seconds)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
