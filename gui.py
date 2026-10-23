#!/usr/bin/env python3
"""Local browser interface for the US state GitHub users project."""
import html
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import github_florida as app

DB_PATH = Path(__file__).with_name("github_users.sqlite3")
CSRF = secrets.token_urlsafe(32)
state = {"busy": False, "status": "Ready"}
lock = threading.Lock()


def stats():
    with app.db(str(DB_PATH)) as conn:
        row = conn.execute("SELECT COUNT(*), COUNT(email), COUNT(phone), COALESCE(SUM(opted_in), 0), COUNT(emailed_at) FROM users").fetchone()
        eligible = conn.execute("""SELECT COUNT(*) FROM users
            WHERE opted_in=1 AND email IS NOT NULL AND emailed_at IS NULL
            AND (email_status IS NULL OR email_status='failed')""").fetchone()[0]
        daily_remaining = app.smtp_daily_remaining(conn)
        queue = conn.execute("SELECT active, last_error FROM email_queue WHERE id=1").fetchone()
        queue_pending = conn.execute("""SELECT COUNT(*) FROM users WHERE opted_in=1
            AND email IS NOT NULL AND emailed_at IS NULL AND email_status IS NULL""").fetchone()[0]
    return {**dict(zip(("users", "emails", "phones", "opted_in", "emailed"), row)),
            "eligible": eligible, "daily_remaining": daily_remaining,
            "queue_active": bool(queue and queue["active"]),
            "queue_pending": queue_pending,
            "queue_error": queue["last_error"] if queue else None}


def profiles(page=1, page_size=25, state="Florida", delivery="all"):
    if state not in app.US_STATES:
        raise ValueError("Choose one of the 50 US states")
    conditions = {
        "all": "1=1",
        "sent": "(emailed_at IS NOT NULL OR email_status='accepted')",
        "not_sent": "(emailed_at IS NULL AND (email_status IS NULL OR email_status='failed'))",
        "unconfirmed": "(emailed_at IS NULL AND email_status='unconfirmed')",
    }
    if delivery not in conditions:
        raise ValueError("Invalid delivery filter")
    where = "state=? AND " + conditions[delivery]
    with app.db(str(DB_PATH)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users WHERE " + where, (state,)).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        rows = conn.execute("""SELECT login, name, location, email, email_source, phone, phone_source,
            state, profile_url, website, social_accounts, opted_in, emailed_at, sent_template,
            email_status, email_status_at, email_error FROM users
            WHERE """ + where + " ORDER BY login COLLATE NOCASE LIMIT ? OFFSET ?",
            (state, page_size, (page - 1) * page_size)).fetchall()
    result = []
    for row in rows:
        profile = dict(row)
        try:
            profile["social_accounts"] = json.loads(profile["social_accounts"] or "[]")
        except json.JSONDecodeError:
            profile["social_accounts"] = []
        result.append(profile)
    return {"items": result, "page": page, "pages": pages, "total": total,
            "state": state, "delivery": delivery}


def email_tests():
    with app.db(str(DB_PATH)) as conn:
        rows = conn.execute("""SELECT recipient, status, template_number, recorded_at
            FROM email_tests ORDER BY id DESC LIMIT 20""").fetchall()
    return [dict(row) for row in rows]


def collection_target():
    with app.db(str(DB_PATH)) as conn:
        row = conn.execute("SELECT state, target FROM collection_job WHERE id=1 AND active=1").fetchone()
    return (row["state"], row["target"]) if row else None


def set_collection_job(state, target, active):
    with app.db(str(DB_PATH)) as conn:
        conn.execute("""INSERT INTO collection_job (id, state, target, active) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state,
            target=excluded.target, active=excluded.active""",
            (state, target, int(active)))
        conn.commit()


def collect_to_target(state_name, target):
    def progress(message):
        with lock:
            state["status"] = message
    failures = 0
    while True:
        with app.db(str(DB_PATH)) as conn:
            before = conn.execute("SELECT COUNT(*) FROM users WHERE state=?", (state_name,)).fetchone()[0]
            remaining = max(0, target - before)
            if not remaining:
                set_collection_job(state_name, target, False)
                return f"{state_name} collection finished with {before} saved users."
            try:
                app.collect(conn, remaining, app.default_github_token(), progress, state=state_name)
            except RuntimeError as exc:
                if not (str(exc).startswith("GitHub network error") or
                        str(exc).startswith("GitHub rate limit")):
                    raise
                failures += 1
                delay = min(30 * (2 ** min(failures - 1, 3)), 240)
                message = f"GitHub connection failed; retrying in {delay}s: {exc}"
                progress(message)
                print(message, flush=True)
                time.sleep(delay)
                continue
            after = conn.execute("SELECT COUNT(*) FROM users WHERE state=?", (state_name,)).fetchone()[0]
        failures = 0
        if after <= before:
            set_collection_job(state_name, target, False)
            return f"{state_name} collection stopped at {after}: no more matching profiles were found."


def start_job(fn):
    with lock:
        if state["busy"]:
            raise ValueError("An operation is already running")
        state.update(busy=True, status="Working…")

    def work():
        try:
            result = fn()
            message = result or "Finished"
        except Exception as exc:
            message = f"Error: {exc}"
            print(message, flush=True)
        with lock:
            state.update(busy=False, status=message)

    threading.Thread(target=work, daemon=True).start()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>US GitHub Users</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#172033;background:#f5f7fb}
*{box-sizing:border-box}body{margin:0;padding:24px}.wrap{width:100%;max-width:1440px;margin:auto}h1{margin:0 0 6px;font-size:28px}p{color:#526079;line-height:1.5}
.card{background:white;border:1px solid #dce2ed;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #1b2a4810}
h2{font-size:19px;margin:0 0 16px}label{display:block;font-weight:600;font-size:14px;margin:12px 0 5px}input,textarea,select{width:100%;font:inherit;border:1px solid #bcc7da;border-radius:8px;padding:10px}textarea{min-height:105px;resize:vertical}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}button,.button{appearance:none;border:0;border-radius:9px;background:#155eef;color:white;font:600 15px system-ui;padding:12px 17px;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.button.secondary{background:#e8eef9;color:#173361}button:disabled{opacity:.55;cursor:wait}.stats{font-weight:600;color:#24406f}#status{padding:12px 15px;background:#e9f2ff;border-radius:8px;color:#173361;margin-top:14px}
small{color:#64718a}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:600px){.split{grid-template-columns:1fr}body{padding:12px}}
.table-scroll{overflow-x:auto;margin-top:14px}table{border-collapse:collapse;width:100%;min-width:1250px}th,td{padding:12px;border-bottom:1px solid #e3e9f2;text-align:left;vertical-align:top;font-size:14px}th{background:#f5f7fb}td a{color:#155eef}td.social a{display:block;margin-bottom:3px}
</style></head><body><main class="wrap">
<h1>US GitHub Users</h1><p>Select a US state to find public profiles, export CSV, and email contacts who have opted in.</p>
<div id="status" role="status">Ready</div>
<div id="queue-status" class="stats">Email queue: checking…</div>
<section class="card"><h2>Profiles</h2><div id="stats" class="stats">Loading counts…</div><label for="delivery-filter">Email delivery</label><select id="delivery-filter" onchange="changeDelivery()"><option value="all">All users</option><option value="sent">Sent</option><option value="not_sent">Not sent</option><option value="unconfirmed">Unconfirmed</option></select><div class="table-scroll"><table><thead><tr><th>GitHub</th><th>Name</th><th>Location</th><th>Email</th><th>Phone</th><th>Website</th><th>Social links</th><th>Consent</th><th>Email result</th></tr></thead><tbody id="profile-rows"><tr><td colspan="9">Loading profiles…</td></tr></tbody></table></div>
<div class="actions"><button id="prev-page" class="secondary" type="button" onclick="changePage(-1)">Previous</button><span id="page-info" style="align-self:center">Page 1 of 1</span><button id="next-page" class="secondary" type="button" onclick="changePage(1)">Next</button></div>
<form method="post" action="/collect"><input type="hidden" name="csrf" value="{csrf}"><label for="state">US state</label><select id="state" name="state" required onchange="changeState()">{state_options}</select><label for="token">GitHub token (optional override)</label><input id="token" name="token" type="password" autocomplete="off" placeholder="Using local default token"><small>Leave blank to use the local default token.</small><div class="actions"><button type="submit">Add up to 500 users in selected state</button><a class="button secondary" href="/export">Export all CSV</a></div></form><form method="post" action="/find-phones"><input type="hidden" name="csrf" value="{csrf}"><div class="actions"><button class="secondary" type="submit">Find public phone numbers</button></div><small>Checks GitHub bios and profile READMEs for labeled phone numbers; processes up to 25 unchecked profiles per click.</small></form></section>
<section class="card"><h2>Test email results</h2><div class="table-scroll"><table><thead><tr><th>Recipient</th><th>SMTP result</th><th>Template</th><th>Recorded</th></tr></thead><tbody id="test-email-rows"><tr><td colspan="4">Loading results…</td></tr></tbody></table></div></section>
<section class="card"><h2>Gmail sender</h2><p>Only opted-in contacts with a public email and no previous delivery are eligible. <strong id="eligible-count">Eligible recipients: 0</strong> <strong id="daily-remaining">App daily allowance: 0</strong></p><form method="post" action="/email" id="email-form"><input type="hidden" name="csrf" value="{csrf}"><div class="split"><div><label for="smtp_user">Gmail address / From address</label><input id="smtp_user" name="smtp_user" type="email" autocomplete="off" placeholder="Use SMTP_USER from .env"></div><div><label for="smtp_password">Gmail app password</label><input id="smtp_password" name="smtp_password" type="password" autocomplete="off" placeholder="Use SMTP_APP_PASSWORD from .env"></div></div><label for="use_templates" style="display:flex;align-items:center;gap:8px"><input id="use_templates" name="use_templates" type="checkbox" value="1" checked style="width:auto" onchange="toggleTemplates()">Randomly choose one of six partnership emails for each recipient</label><p>The selected sender address is used in the From header and inside the email. Leave both fields blank to use .env. Messages are spaced 120 seconds apart, with at most 100 app sends per rolling 24 hours. Telegram is removed. Template 6 is a follow-up and may be chosen for a first email.</p><div id="manual-fields" style="display:none"><label for="subject">Subject</label><input id="subject" name="subject"><label for="body">Message</label><textarea id="body" name="body"></textarea></div><div class="actions"><button class="secondary" name="action" value="preview" formnovalidate>Preview recipients</button><button name="action" value="send" onclick="return confirm('Send to eligible contacts at 120-second intervals?')">Send emails</button></div></form></section>
</main><script>
function link(url,label){const a=document.createElement('a');if(typeof url==='string' && /^https?:\/\//i.test(url)){a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=label||url;return a}return document.createTextNode(label||'—')}
function cell(row,value){const td=document.createElement('td');td.textContent=value??'—';row.appendChild(td);return td}
function toggleTemplates(){document.getElementById('manual-fields').style.display=document.getElementById('use_templates').checked?'none':'block'}
let currentPage=1;
try{const savedState=localStorage.getItem('selectedState');if(savedState&&[...document.getElementById('state').options].some(option=>option.value===savedState))document.getElementById('state').value=savedState}catch(e){}
function changeState(){try{localStorage.setItem('selectedState',document.getElementById('state').value)}catch(e){}currentPage=1;update()}
function changeDelivery(){currentPage=1;update()}
function changePage(delta){currentPage=Math.max(1,currentPage+delta);update()}
function emailResult(p){
  if(p.emailed_at||p.email_status==='accepted')return `Accepted by Gmail SMTP · ${new Date(p.emailed_at||p.email_status_at).toLocaleString()}${p.sent_template?` · Template ${p.sent_template}`:''}`;
  if(p.email_status==='failed')return `Rejected · ${new Date(p.email_status_at).toLocaleString()}`;
  if(p.email_status==='unconfirmed')return `Unconfirmed · ${new Date(p.email_status_at).toLocaleString()}`;
  return p.email?'Not sent':'No email';
}
async function update(){
  try{
    const selectedState=document.getElementById('state').value;
    const delivery=document.getElementById('delivery-filter').value;
    const [sr,pr,tr]=await Promise.all([fetch('/status'),fetch(`/profiles?page=${currentPage}&state=${encodeURIComponent(selectedState)}&delivery=${encodeURIComponent(delivery)}`),fetch('/email-tests')]);
    const x=await sr.json(),result=await pr.json(),people=result.items,tests=await tr.json();
    currentPage=result.page;
    document.getElementById('stats').textContent=`${selectedState} users: ${result.total} | All users: ${x.users} | Emails: ${x.emails} | Phones: ${x.phones} | Opted in: ${x.opted_in} | Accepted: ${x.emailed}`;
    document.getElementById('eligible-count').textContent=`Eligible recipients: ${x.eligible}`;
    document.getElementById('daily-remaining').textContent=`App daily allowance: ${x.daily_remaining}`;
    document.getElementById('status').textContent=x.status;
    document.getElementById('queue-status').textContent=`Email queue: ${x.queue_active?'running':'stopped'} | Remaining: ${x.queue_pending}${x.queue_error?` | Last error: ${x.queue_error}`:''}`;
    document.querySelectorAll('button').forEach(b=>b.disabled=x.busy);
    document.getElementById('page-info').textContent=`Page ${result.page} of ${result.pages} (${result.total} users)`;
    document.getElementById('prev-page').disabled=result.page<=1;
    document.getElementById('next-page').disabled=result.page>=result.pages;
    const body=document.getElementById('profile-rows');body.replaceChildren();
    if(!people.length){const tr=document.createElement('tr');cell(tr,'No saved profiles yet').colSpan=9;body.appendChild(tr)}
    for(const p of people){
      const tr=document.createElement('tr');const github=cell(tr,'');github.appendChild(link(p.profile_url,p.login));
      cell(tr,p.name);cell(tr,p.location);
      const email=cell(tr,p.email);if(p.email_source==='user_provided')email.title='Email supplied manually';
      const phone=cell(tr,p.phone);if(p.phone_source)phone.title=`Source: ${p.phone_source}`;
      const website=cell(tr,'');website.appendChild(link(p.website,p.website||'—'));
      const social=cell(tr,'');social.className='social';
      if(p.social_accounts.length){for(const account of p.social_accounts)social.appendChild(link(account.url,account.provider||account.url))}else social.textContent='—';
      cell(tr,p.opted_in?'Yes':'No');
      const resultCell=cell(tr,emailResult(p));if(p.email_error)resultCell.title=p.email_error;
      body.appendChild(tr);
    }
    const testBody=document.getElementById('test-email-rows');testBody.replaceChildren();
    if(!tests.length){const tr=document.createElement('tr');cell(tr,'No test emails recorded').colSpan=4;testBody.appendChild(tr)}
    for(const item of tests){const tr=document.createElement('tr');cell(tr,item.recipient);cell(tr,item.status);cell(tr,item.template_number?`Template ${item.template_number}`:'—');cell(tr,new Date(item.recorded_at).toLocaleString());testBody.appendChild(tr)}
  }catch(e){document.getElementById('status').textContent='Unable to reach the local app.'}
}
update();setInterval(update,2000)
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid recording form values or secrets in server logs.
        pass

    def respond(self, code, body, content_type="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self):
        self.respond(303, b"", extra={"Location": "/"})

    def do_GET(self):
        if self.path == "/":
            options = "".join(
                f'<option value="{html.escape(name, quote=True)}"'
                f'{" selected" if name == "Florida" else ""}>{html.escape(name)}</option>'
                for name in app.US_STATES)
            self.respond(200, PAGE.replace("{csrf}", html.escape(CSRF, quote=True))
                         .replace("{state_options}", options))
        elif self.path == "/status":
            with lock:
                current = dict(state)
            self.respond(200, json.dumps({**current, **stats()}), "application/json")
        elif urlsplit(self.path).path == "/profiles":
            query = parse_qs(urlsplit(self.path).query)
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            state_name = query.get("state", ["Florida"])[0]
            if state_name not in app.US_STATES:
                self.respond(400, json.dumps({"error": "Invalid US state"}), "application/json")
                return
            delivery = query.get("delivery", ["all"])[0]
            if delivery not in ("all", "sent", "not_sent", "unconfirmed"):
                self.respond(400, json.dumps({"error": "Invalid delivery filter"}), "application/json")
                return
            self.respond(200, json.dumps(profiles(page, state=state_name, delivery=delivery), ensure_ascii=False), "application/json")
        elif self.path == "/email-tests":
            self.respond(200, json.dumps(email_tests(), ensure_ascii=False), "application/json")
        elif self.path == "/export":
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "us_github_users.csv"
                with app.db(str(DB_PATH)) as conn:
                    app.export_csv(conn, str(path))
                self.respond(200, path.read_bytes(), "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="us_github_users.csv"'})
        else:
            self.respond(404, "Not found")

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > 1_000_000:
            self.respond(413, "Invalid request size")
            return
        form = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        value = lambda name: form.get(name, [""])[0]
        if not secrets.compare_digest(value("csrf"), CSRF):
            self.respond(403, "Invalid form token")
            return
        try:
            if self.path == "/collect":
                token = value("token").strip() or app.default_github_token()
                state_name = value("state")
                if state_name not in app.US_STATES:
                    raise ValueError("Choose one of the 50 US states")
                with app.db(str(DB_PATH)) as conn:
                    current_count = conn.execute("SELECT COUNT(*) FROM users WHERE state=?", (state_name,)).fetchone()[0]
                target = current_count + 500
                set_collection_job(state_name, target, True)
                def task():
                    if token != app.default_github_token():
                        def progress(message):
                            with lock:
                                state["status"] = message
                        with app.db(str(DB_PATH)) as conn:
                            remaining = max(0, target - conn.execute("SELECT COUNT(*) FROM users WHERE state=?", (state_name,)).fetchone()[0])
                            if remaining:
                                app.collect(conn, remaining, token, progress, state=state_name)
                        set_collection_job(state_name, target, False)
                        return f"{state_name} collection finished. Use Export CSV to download profiles."
                    return collect_to_target(state_name, target)
                start_job(task)
            elif self.path == "/find-phones":
                def task():
                    def progress(message):
                        with lock:
                            state["status"] = message
                    with app.db(str(DB_PATH)) as conn:
                        checked, found, failed = app.enrich_phones(conn, 25, app.default_github_token(), progress)
                    return f"Checked {checked} GitHub profiles; found {found} public phone numbers; {failed} lookup failures."
                start_job(task)
            elif self.path == "/consent":
                login = value("login").strip()
                if not login:
                    raise ValueError("Enter a GitHub login")
                opted_in = value("action") == "opt_in"
                with app.db(str(DB_PATH)) as conn:
                    result = conn.execute("UPDATE users SET opted_in=? WHERE login=?", (int(opted_in), login))
                    conn.commit()
                if not result.rowcount:
                    raise ValueError("GitHub login not found in the database")
                with lock:
                    state["status"] = f"Consent {'recorded' if opted_in else 'revoked'} for {login}."
            elif self.path == "/email":
                if value("action") == "preview":
                    with lock:
                        current = stats()
                        state["status"] = f"Preview: {min(current['eligible'], current['daily_remaining'], 100)} eligible recipients within today's app allowance."
                    self.redirect()
                    return
                templates = app.load_email_templates() if value("use_templates") == "1" else None
                subject, body = value("subject").strip(), value("body").strip()
                if templates is None and (not subject or not body):
                    raise ValueError("Enter a subject and message")
                live = value("action") == "send"
                default_user, default_password = app.smtp_credentials()
                user = value("smtp_user").strip() or default_user
                password = value("smtp_password") or default_password
                if live and (not user or not password):
                    raise ValueError("Enter Gmail credentials or set them in .env")
                def progress(message):
                    with lock:
                        state["status"] = message
                def task():
                    if templates:
                        with app.db(str(DB_PATH)) as conn:
                            count = app.send(conn, None, None, 100, live, user, password,
                                             progress, templates)
                    else:
                        with tempfile.TemporaryDirectory() as folder:
                            path = Path(folder) / "message.txt"
                            path.write_text(body, encoding="utf-8")
                            with app.db(str(DB_PATH)) as conn:
                                count = app.send(conn, subject, str(path), 100, live, user, password,
                                                 progress)
                    if live:
                        return f"Accepted {count} emails by Gmail SMTP." if count else "No eligible recipients within the current app sending allowance."
                    return f"Preview: {count} eligible recipients."
                start_job(task)
            else:
                self.respond(404, "Not found")
                return
            self.redirect()
        except ValueError as exc:
            with lock:
                state["status"] = f"Error: {exc}"
            self.redirect()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    url = "http://127.0.0.1:8765"
    print(f"US GitHub Users running at {url}", flush=True)
    job = collection_target()
    if job is not None:
        state_name, target = job
        print(f"Resuming {state_name} collection toward {target} users", flush=True)
        start_job(lambda: collect_to_target(state_name, target))
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
