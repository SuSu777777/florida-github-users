#!/usr/bin/env python3
"""Collect public GitHub profiles in Florida and email opted-in contacts."""
import argparse
import csv
import json
import os
import sqlite3
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

API = "https://api.github.com"
REQUEST_INTERVAL_SECONDS = 1.0
_request_lock = threading.Lock()
_last_request_finished = None
EXPORT_COLUMNS = ("github_id", "login", "name", "location", "email",
                  "email_source", "profile_url", "website", "social_accounts",
                  "fetched_at", "opted_in", "emailed_at")


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
        location TEXT, email TEXT, profile_url TEXT NOT NULL,
        fetched_at TEXT NOT NULL, opted_in INTEGER NOT NULL DEFAULT 0,
        emailed_at TEXT
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name in ("email_source", "website", "social_accounts"):
        if name not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS collection_job (
        id INTEGER PRIMARY KEY CHECK (id = 1), target INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0
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
                                   "--header", "User-Agent: florida-github-contacts/1.0",
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


def collect(conn, count, token, progress=None):
    if not 1 <= count <= 500:
        raise ValueError("count must be between 1 and 500")
    saved = 0
    skipped = 0
    for page in range(1, 11):
        query = urllib.parse.urlencode({"q": "location:Florida type:user", "per_page": 100, "page": page})
        items = get_json("/search/users?" + query, token, progress).get("items", [])
        if not items:
            break
        for item in items:
            if conn.execute("SELECT 1 FROM users WHERE github_id=?", (item["id"],)).fetchone():
                continue
            try:
                profile = get_json("/users/" + urllib.parse.quote(item["login"]), token, progress)
            except RuntimeError as exc:
                skipped += 1
                if progress:
                    progress(f"Skipped {item['login']} after GitHub request failure: {exc}")
                continue
            # Search location is a user-supplied string; retain only explicit Florida matches.
            location = (profile.get("location") or "").strip()
            if "florida" not in location.casefold():
                continue
            try:
                accounts = get_social_accounts(item["login"], token, progress)
            except RuntimeError as exc:
                accounts = []
                if progress:
                    progress(f"Social account lookup failed for {item['login']}: {exc}")
            if profile.get("twitter_username"):
                accounts.append({"provider": "twitter", "url": "https://x.com/" + profile["twitter_username"]})
            conn.execute("""INSERT INTO users
                (github_id, login, name, location, email, email_source, profile_url,
                 website, social_accounts, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(github_id) DO UPDATE SET
                login=excluded.login, name=excluded.name, location=excluded.location,
                email=COALESCE(excluded.email, users.email),
                email_source=COALESCE(excluded.email_source, users.email_source),
                profile_url=excluded.profile_url, website=excluded.website,
                social_accounts=excluded.social_accounts,
                fetched_at=excluded.fetched_at""",
                (profile["id"], profile["login"], profile.get("name"), location,
                 profile.get("email"), "github_profile" if profile.get("email") else None,
                 profile["html_url"], profile.get("blog"), json.dumps(accounts, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()))
            saved += 1
            conn.commit()
            if progress:
                progress(f"Saved {saved}/{count} Florida profiles")
            if saved % 25 == 0:
                print(f"Saved {saved}/{count}", flush=True)
            if saved >= count:
                break
        conn.commit()
        if saved >= count:
            break
    print(f"Saved {saved} Florida profiles; {conn.execute('SELECT COUNT(*) FROM users WHERE email IS NOT NULL').fetchone()[0]} database profiles have email addresses.")
    if skipped:
        print(f"Skipped {skipped} profiles after repeated GitHub request failures.", file=sys.stderr)
    if saved < count:
        print("Fewer matching profiles were available in GitHub's first 1,000 search results.", file=sys.stderr)


def send(conn, subject, body_path, limit, live, user=None, password=None):
    user = user or os.getenv("SMTP_USER")
    password = password or os.getenv("SMTP_APP_PASSWORD")
    if live and (not user or not password):
        raise ValueError("Set SMTP_USER and SMTP_APP_PASSWORD before sending")
    body = open(body_path, encoding="utf-8").read()
    if not subject or not body.strip():
        raise ValueError("subject and message body are required")
    rows = conn.execute("""SELECT github_id, login, email FROM users
        WHERE opted_in = 1 AND email IS NOT NULL AND emailed_at IS NULL
        ORDER BY github_id LIMIT ?""", (limit,)).fetchall()
    if not live:
        print(f"Dry run: {len(rows)} opted-in, unsent recipients. Add --send to deliver.")
        return
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        for row in rows:
            msg = EmailMessage()
            msg["From"] = user
            msg["To"] = row["email"]
            msg["Subject"] = subject
            msg.set_content(body)
            smtp.send_message(msg)
            conn.execute("UPDATE users SET emailed_at=? WHERE github_id=?",
                         (datetime.now(timezone.utc).isoformat(), row["github_id"]))
            conn.commit()
            print(f"Sent to {row['login']}")


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
    commands.add_parser("stats")
    export_cmd = commands.add_parser("export", help="Export stored users to CSV")
    export_cmd.add_argument("--output", default="florida_github_users.csv")
    consent_cmd = commands.add_parser("consent", help="Record verified recipient consent")
    consent_cmd.add_argument("login")
    consent_cmd.add_argument("--revoke", action="store_true")
    send_cmd = commands.add_parser("send")
    send_cmd.add_argument("--subject", required=True)
    send_cmd.add_argument("--body-file", required=True)
    send_cmd.add_argument("--limit", type=int, default=100)
    send_cmd.add_argument("--send", action="store_true", help="Actually deliver; default is dry run")
    args = parser.parse_args()
    try:
        conn = db(args.db)
        if args.command == "collect":
            collect(conn, args.count, default_github_token())
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
        elif args.command == "send":
            if args.limit < 1:
                raise ValueError("limit must be positive")
            send(conn, args.subject, args.body_file, args.limit, args.send)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
