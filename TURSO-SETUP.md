# Turso setup (keeps your data when Render restarts, sleeps or redeploys)

Turso's labels change from time to time; if a button is named slightly differently, look for the closest one. **Nothing here has been tested against a real Turso account yet** — step 3 is the test.

## 1. Create the account and a TEST database
1. Sign up at **turso.tech**.
2. Create a database named `winter-arc-test`. Choose the location **closest to Render's region (Oregon, USA)**.
3. Copy its **URL** (looks like `libsql://winter-arc-test-yourname.turso.io`).
4. Create an **auth token** for it (dashboard "Generate token", or `turso db tokens create winter-arc-test`). Treat it like a password.

## 2. Install the project locally
```
python -m venv venv
venv\Scripts\activate             (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
```

## 3. THE connection test (do this before touching Render)
```
set TURSO_DATABASE_URL=libsql://winter-arc-test-yourname.turso.io          (Mac/Linux: export ...)
set TURSO_AUTH_TOKEN=paste-the-token-here
python tools/turso_check.py
```
It prints PASS/FAIL lines (connection speed, writes, rollbacks, concurrent transactions, reconnecting after a dropped connection, an interrupted transaction, a backup snapshot) and ends with **ALL REQUIRED CHECKS PASSED** or a list of failures. It uses its own temporary tables and never prints the token. **Send me the output** if anything fails.

## 4. Practice a restore into the TEST database (recommended)
Use a backup file from `/backup` or the backup group:
```
python tools/restore_backup.py winter-arc-backup-....db.gz
python tools/turso_check.py --counts        (shows the restored row counts)
```

## 5. Create the real database
Create `winter-arc` (same location) and its own token. Never reuse the test database or token for the real challenge.

## 6. Render → your service → Environment (add these; keep your existing ones)
| Name | Value |
|---|---|
| `TURSO_DATABASE_URL` | `libsql://winter-arc-<yourname>.turso.io` |
| `TURSO_AUTH_TOKEN` | the real database's token (type it only here) |
| `BACKUP_CHAT_ID` | the backup group's ID — see `BACKUP-RESTORE.md` |
| optional `BACKUP_EVERY_HOURS` | default `24` |
| optional `BACKUP_KEEP_DAYS` | default `30` |

Keep `BOT_TOKEN`, `ADMIN_IDS`, `WEB_ADMIN_PASSWORD`, `WEB_SECRET` as they are. Both `TURSO_*` variables are required together; one alone makes the app refuse to start. Saving variables redeploys the service.

## 7. After the redeploy
* Logs: `Database ready (schema v1) · Turso (…turso.io)` and `Bot started as @OG_WintArcbot`. The admins' Telegram startup message shows "Database: Turso (…)".
* Register a test participant, then in Render choose **Manual Deploy / Restart** and check the participant is **still there** (this is the proof that data now survives).
* If Turso is unreachable the app retries for about 35 seconds, then stops with an error so Render restarts it. It **never** creates a local database instead.

## What Turso does not change
Render Free still sleeps after about 15 idle minutes (the bot is offline while asleep). Turso keeps the data safe across those sleeps.
