# Florida GitHub users

A local Python CLI that searches GitHub for users whose public profile location contains `Florida`, stores up to 500 per run in SQLite, and can send email through Gmail SMTP to contacts whose consent you have recorded.

Requires Python 3.9+ and `curl`. No Python packages are required.

## Desktop window

Run `./start_project.sh` to start the local interface in your browser at `http://127.0.0.1:8765`. On macOS, the server runs as a user LaunchAgent independently of the terminal and writes errors to `logs/project.log`. Use `./stop_project.sh` to stop it. If the server exits during a collection, the service restarts it and resumes toward 500 saved users. The **Profiles** section shows stored users in a table with email, website, social links, and consent status, 25 users per page with Previous and Next buttons. Use **Collect 500 users** and **Export CSV**. The GitHub token field can be left blank when `GITHUB_TOKEN` is set in the environment or in the ignored local `local_config.py` file. The page also has buttons to record verified consent, preview eligible email recipients, and send through Gmail SMTP. Gmail credentials entered on the page are held in memory for that operation and are not saved to the project.

If GitHub's connection times out, collection waits and retries automatically. Profiles already saved in SQLite are retained and skipped on the next pass.

```sh
export GITHUB_TOKEN='your-read-only-token'
python3 github_florida.py collect --count 500
python3 github_florida.py stats
```

A token with no special permissions is sufficient. The script can run without one, but GitHub's unauthenticated limits may interrupt a 500-profile collection. GitHub search pages contain at most 100 users, so the script fetches several pages and then each public profile and its public social accounts. GitHub requests run one at a time, with at least one second between the end of a request and the start of the next. A 500-profile run can therefore take more than 17 minutes. Public website and social links are saved. Only publicly visible profile email addresses are fetched from GitHub; many users have no public email. Repeated runs update profiles by GitHub ID. An email entered manually is retained if a later GitHub response has no email. The database is `github_users.sqlite3` by default, or set `--db PATH`.

## Export CSV

```sh
python3 github_florida.py export
python3 github_florida.py export --output users.csv
```

The export includes all stored profiles, with columns for GitHub ID, login, name, location, email, email source, profile URL, website, public social accounts (JSON), fetch time, consent status, and email send time. It writes a header even when the database is empty. Spreadsheet formula characters in profile text are escaped when exported.

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

To deliver, set Gmail credentials and add `--send`:

```sh
export SMTP_USER='you@gmail.com'
export SMTP_APP_PASSWORD='your-gmail-app-password'
python3 github_florida.py send --subject 'Your subject' --body-file message.txt --send
```

Sending uses `smtp.gmail.com:465`, defaults to at most 100 messages per run, and records each successful delivery to prevent repeat sends. Only records marked opted in, with a public email and no prior delivery, are eligible. Store credentials in environment variables; never commit them to source control.
