# US state GitHub users

A local Python CLI that searches GitHub for users whose public profile location names a selected US state, stores them in SQLite in batches of up to 500, and can send email through Gmail SMTP to contacts whose consent you have recorded.

Requires Python 3.9+ and `curl`. No Python packages are required.

## Desktop window

Run `./start_project.sh` to start the local interface in your browser at `http://127.0.0.1:8765`. On macOS, the server runs as a user LaunchAgent independently of the terminal and writes errors to `logs/project.log`. Use `./stop_project.sh` to stop it. If the server exits during a collection, the service restarts it for the selected state. The **Profiles** section lists all 50 US states. Choose one to view saved users or add up to 500 more in that state each time you click; the table shows 25 users per page with Previous and Next buttons. **Export all CSV** includes every saved state. The GitHub token field can be left blank when `GITHUB_TOKEN` is set in the environment or in the ignored local `local_config.py` file. The page also has buttons to preview eligible email recipients and send through Gmail SMTP. Gmail credentials entered on the page are held in memory for that operation and are not saved to the project.

If GitHub's connection times out, collection waits and retries automatically. Profiles already saved in SQLite are retained and skipped on the next pass.

```sh
export GITHUB_TOKEN='your-read-only-token'
python3 github_florida.py collect --count 500
python3 github_florida.py collect --state 'New York' --count 500
python3 github_florida.py stats
```

A token with no special permissions is sufficient. The script can run without one, but GitHub's unauthenticated limits may interrupt a 500-profile collection. GitHub search pages contain at most 100 users, so the script fetches several pages and then each public profile and its public social accounts. GitHub requests run one at a time, with at least one second between the end of a request and the start of the next. A 500-profile run can therefore take more than 17 minutes. Public website and social links are saved. Only publicly visible profile email addresses are fetched from GitHub; many users have no public email. Repeated runs add new profiles by GitHub ID. Search progress is saved per state, so later runs can continue beyond GitHub's first 1,000 results for a query. An email entered manually is retained if a later GitHub response has no email. The database is `github_users.sqlite3` by default, or set `--db PATH`.

## Export CSV

```sh
python3 github_florida.py export
python3 github_florida.py export --output users.csv
```

The export includes all stored profiles, with columns for GitHub ID, login, name, location, selected state, email, email source, public phone number, phone source, profile URL, website, public social accounts (JSON), fetch time, consent status, and email send status, time, and template. It writes a header even when the database is empty. Spreadsheet formula characters in profile text are escaped when exported.

The collector checks each user's public GitHub bio and, if needed, their GitHub profile README for an explicitly labeled US phone number. Existing saved users can be checked with **Find public phone numbers** in the browser (25 unchecked profiles per click) or `python3 github_florida.py find-phones --limit 25`. This uses GitHub only, and adds up to one README API request per profile; the source link is stored with each number.

## Email opted-in contacts

Record consent only when you have independently verified it:

```sh
python3 github_florida.py consent USERNAME
python3 github_florida.py consent USERNAME --revoke
```

Create a plain-text message, then inspect the recipient count with a dry run:

```sh
python3 github_florida.py send --subject 'Your subject' --body-file message.txt
```

To deliver, create a local `.env` file in the project directory with these two lines, then add `--send`:

```text
SMTP_USER=you@gmail.com
SMTP_APP_PASSWORD=your-gmail-app-password
```

Restrict the file to your account with `chmod 600 .env`. The file is ignored by Git. Process environment variables override values in `.env`. Then run:

```sh
python3 github_florida.py send --subject 'Your subject' --body-file message.txt --send
```

Sending uses `smtp.gmail.com:465`, waits 120 seconds between messages, and allows at most 100 app-recorded sends in any rolling 24 hours. CLI users can raise the interval with `--interval-seconds`; 60 seconds is the minimum. These controls leave room below Gmail's personal-account daily limit but cannot guarantee Gmail will accept every message, especially if you also send mail outside this app. Each message accepted by Gmail SMTP is recorded to prevent repeat sends. SMTP acceptance does not prove inbox delivery. Only records marked opted in, with a public email and no prior acceptance, are eligible. A rejected recipient is marked failed and may be retried; an uncertain result is marked unconfirmed and excluded from automatic retries to avoid duplicates. Store credentials in `.env` or process environment variables; never commit them to source control.

For a long sequential campaign, `./start_bulk_sending.sh` starts a macOS LaunchAgent that checks the queue every 120 seconds and sends at most one eligible, unattempted message per check. It waits for the rolling app allowance before continuing on later days and resumes after login. The browser shows queue status and each profile's SMTP result. `./stop_bulk_sending.sh` stops this queue. Recipient rejections are recorded and skipped by the queue; uncertain outcomes are held for review. Three consecutive SMTP errors pause the queue.

In the browser, **Preview recipients** shows the eligible count without Gmail credentials. The Gmail fields are optional when `.env` is configured; values entered in the form override the file for that send. Click **Send emails** to deliver to up to 100 eligible contacts. By default, each recipient gets one randomly selected message from `email_templates.json`, imported from `Development_Team_Partnership_Email_Templates.docx`. The selected Gmail address is used as both the From header and the email address inside each message. The sender's name is Wong, the greeting uses each recipient's GitHub username, and the message says their GitHub projects made a strong impression. Telegram references have been removed. Uncheck the template option to enter a custom subject and message. The page shows send progress and the final count. Template 6 is a follow-up and can be selected for an initial email under random selection.

The profile table shows Gmail SMTP acceptance, recipient rejection, or an unconfirmed send result for each saved GitHub user. One-off test sends to addresses outside the profile database appear in the separate **Test email results** table when recorded with `record_email_test`.

To refresh the six templates from the Word document, run `python3 import_templates.py /path/to/Development_Team_Partnership_Email_Templates.docx`. The importer uses Python's standard library and writes clean templates to `email_templates.json`. CLI sending with random templates uses `python3 github_florida.py send --templates` for a dry run and adds `--send` for delivery, with `SMTP_USER` and `SMTP_APP_PASSWORD` set.
