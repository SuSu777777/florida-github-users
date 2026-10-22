#!/usr/bin/env python3
"""Local browser interface for the Florida GitHub users project."""
import html
import json
import os
import secrets
import tempfile
import threading
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
        row = conn.execute("SELECT COUNT(*), COUNT(email), COALESCE(SUM(opted_in), 0), COUNT(emailed_at) FROM users").fetchone()
    return dict(zip(("users", "emails", "opted_in", "emailed"), row))


def profiles(page=1, page_size=25):
    with app.db(str(DB_PATH)) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        rows = conn.execute("""SELECT login, name, location, email, email_source,
            profile_url, website, social_accounts, opted_in FROM users
            ORDER BY login COLLATE NOCASE LIMIT ? OFFSET ?""",
            (page_size, (page - 1) * page_size)).fetchall()
    result = []
    for row in rows:
        profile = dict(row)
        try:
            profile["social_accounts"] = json.loads(profile["social_accounts"] or "[]")
        except json.JSONDecodeError:
            profile["social_accounts"] = []
        result.append(profile)
    return {"items": result, "page": page, "pages": pages, "total": total}


def collection_target():
    with app.db(str(DB_PATH)) as conn:
        row = conn.execute("SELECT target FROM collection_job WHERE id=1 AND active=1").fetchone()
    return row[0] if row else None


def set_collection_job(target, active):
    with app.db(str(DB_PATH)) as conn:
        conn.execute("""INSERT INTO collection_job (id, target, active) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET target=excluded.target, active=excluded.active""",
            (target, int(active)))
        conn.commit()


def collect_to_target(target):
    def progress(message):
        with lock:
            state["status"] = message
    with app.db(str(DB_PATH)) as conn:
        remaining = max(0, target - conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        if remaining:
            app.collect(conn, remaining, app.default_github_token(), progress)
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    set_collection_job(target, False)
    return f"Collection finished with {total} saved users."


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
<title>Florida GitHub Users</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#172033;background:#f5f7fb}
*{box-sizing:border-box}body{margin:0;padding:24px}.wrap{width:100%;max-width:1440px;margin:auto}h1{margin:0 0 6px;font-size:28px}p{color:#526079;line-height:1.5}
.card{background:white;border:1px solid #dce2ed;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #1b2a4810}
h2{font-size:19px;margin:0 0 16px}label{display:block;font-weight:600;font-size:14px;margin:12px 0 5px}input,textarea{width:100%;font:inherit;border:1px solid #bcc7da;border-radius:8px;padding:10px}textarea{min-height:105px;resize:vertical}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}button,.button{appearance:none;border:0;border-radius:9px;background:#155eef;color:white;font:600 15px system-ui;padding:12px 17px;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.button.secondary{background:#e8eef9;color:#173361}button:disabled{opacity:.55;cursor:wait}.stats{font-weight:600;color:#24406f}#status{padding:12px 15px;background:#e9f2ff;border-radius:8px;color:#173361;margin-top:14px}
small{color:#64718a}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:600px){.split{grid-template-columns:1fr}body{padding:12px}}
.table-scroll{overflow-x:auto;margin-top:14px}table{border-collapse:collapse;width:100%;min-width:1100px}th,td{padding:12px;border-bottom:1px solid #e3e9f2;text-align:left;vertical-align:top;font-size:14px}th{background:#f5f7fb}td a{color:#155eef}td.social a{display:block;margin-bottom:3px}
</style></head><body><main class="wrap">
<h1>Florida GitHub Users</h1><p>Collect public profiles, export CSV, and email contacts who have opted in.</p>
<div id="status" role="status">Ready</div>
<section class="card"><h2>Profiles</h2><div id="stats" class="stats">Loading counts…</div><div class="table-scroll"><table><thead><tr><th>GitHub</th><th>Name</th><th>Location</th><th>Email</th><th>Website</th><th>Social links</th><th>Consent</th></tr></thead><tbody id="profile-rows"><tr><td colspan="7">Loading profiles…</td></tr></tbody></table></div>
<div class="actions"><button id="prev-page" class="secondary" type="button" onclick="changePage(-1)">Previous</button><span id="page-info" style="align-self:center">Page 1 of 1</span><button id="next-page" class="secondary" type="button" onclick="changePage(1)">Next</button></div>
<form method="post" action="/collect"><input type="hidden" name="csrf" value="{csrf}"><label for="token">GitHub token (optional override)</label><input id="token" name="token" type="password" autocomplete="off" placeholder="Using local default token"><small>Leave blank to use the local default token.</small><div class="actions"><button type="submit">Collect 500 users</button><a class="button secondary" href="/export">Export CSV</a></div></form></section>
<section class="card"><h2>Email consent</h2><p>Record consent only when the recipient has explicitly agreed to email.</p><form method="post" action="/consent"><input type="hidden" name="csrf" value="{csrf}"><label for="login">GitHub login</label><input id="login" name="login" required><div class="actions"><button name="action" value="opt_in">Mark opted in</button><button class="secondary" name="action" value="revoke">Revoke consent</button></div></form></section>
<section class="card"><h2>Gmail sender</h2><p>Only opted-in contacts with a public email and no previous delivery are eligible.</p><form method="post" action="/email" id="email-form"><input type="hidden" name="csrf" value="{csrf}"><div class="split"><div><label for="smtp_user">Gmail address</label><input id="smtp_user" name="smtp_user" type="email" autocomplete="off"></div><div><label for="smtp_password">Gmail app password</label><input id="smtp_password" name="smtp_password" type="password" autocomplete="off"></div></div><label for="subject">Subject</label><input id="subject" name="subject" required><label for="body">Message</label><textarea id="body" name="body" required></textarea><div class="actions"><button class="secondary" name="action" value="preview">Preview recipients</button><button name="action" value="send" onclick="return confirm('Send to up to 100 opted-in contacts?')">Send emails</button></div></form></section>
</main><script>
function link(url,label){const a=document.createElement('a');if(typeof url==='string' && /^https?:\/\//i.test(url)){a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=label||url;return a}return document.createTextNode(label||'—')}
function cell(row,value){const td=document.createElement('td');td.textContent=value??'—';row.appendChild(td);return td}
let currentPage=1;
function changePage(delta){currentPage=Math.max(1,currentPage+delta);update()}
async function update(){try{const [sr,pr]=await Promise.all([fetch('/status'),fetch(`/profiles?page=${currentPage}`)]);const x=await sr.json(),result=await pr.json(),people=result.items;currentPage=result.page;document.getElementById('stats').textContent=`Users: ${x.users} | Emails: ${x.emails} | Opted in: ${x.opted_in} | Emailed: ${x.emailed}`;document.getElementById('status').textContent=x.status;document.querySelectorAll('button').forEach(b=>b.disabled=x.busy);document.getElementById('page-info').textContent=`Page ${result.page} of ${result.pages} (${result.total} users)`;document.getElementById('prev-page').disabled=result.page<=1;document.getElementById('next-page').disabled=result.page>=result.pages;const body=document.getElementById('profile-rows');body.replaceChildren();if(!people.length){const tr=document.createElement('tr');cell(tr,'No saved profiles yet').colSpan=7;body.appendChild(tr)}for(const p of people){const tr=document.createElement('tr');const github=cell(tr,'');github.appendChild(link(p.profile_url,p.login));cell(tr,p.name);cell(tr,p.location);const email=cell(tr,p.email);if(p.email_source==='user_provided')email.title='Email supplied manually';const website=cell(tr,'');website.appendChild(link(p.website,p.website||'—'));const social=cell(tr,'');social.className='social';if(p.social_accounts.length){for(const account of p.social_accounts)social.appendChild(link(account.url,account.provider||account.url))}else social.textContent='—';cell(tr,p.opted_in?'Yes':'No');body.appendChild(tr)}}catch(e){document.getElementById('status').textContent='Unable to reach the local app.'}}
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
            self.respond(200, PAGE.replace("{csrf}", html.escape(CSRF, quote=True)))
        elif self.path == "/status":
            with lock:
                current = dict(state)
            self.respond(200, json.dumps({**current, **stats()}), "application/json")
        elif urlsplit(self.path).path == "/profiles":
            try:
                page = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
            except ValueError:
                page = 1
            self.respond(200, json.dumps(profiles(page), ensure_ascii=False), "application/json")
        elif self.path == "/export":
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "florida_github_users.csv"
                with app.db(str(DB_PATH)) as conn:
                    app.export_csv(conn, str(path))
                self.respond(200, path.read_bytes(), "text/csv; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="florida_github_users.csv"'})
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
                target = 500
                set_collection_job(target, True)
                def task():
                    if token != app.default_github_token():
                        def progress(message):
                            with lock:
                                state["status"] = message
                        with app.db(str(DB_PATH)) as conn:
                            remaining = max(0, target - conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                            if remaining:
                                app.collect(conn, remaining, token, progress)
                        set_collection_job(target, False)
                        return "Collection finished. Use Export CSV to download profiles."
                    return collect_to_target(target)
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
                subject, body = value("subject").strip(), value("body").strip()
                if not subject or not body:
                    raise ValueError("Enter a subject and message")
                live = value("action") == "send"
                user, password = value("smtp_user").strip(), value("smtp_password")
                if live and (not user or not password):
                    raise ValueError("Enter your Gmail address and app password")
                def task():
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "message.txt"
                        path.write_text(body, encoding="utf-8")
                        with app.db(str(DB_PATH)) as conn:
                            app.send(conn, subject, str(path), 100, live, user, password)
                    return "Email operation finished."
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
    print(f"Florida GitHub Users running at {url}", flush=True)
    target = collection_target()
    if target is not None:
        print(f"Resuming collection toward {target} users", flush=True)
        start_job(lambda: collect_to_target(target))
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
