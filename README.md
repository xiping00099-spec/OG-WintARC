# WINTER ARC — Telegram bot + website

Study-challenge manager for THE OG ASSOCIATION: a Telegram bot (targets, reports, admin approval, points, penalties, streaks,
reminders, charts, XLSX export) and a premium website (dashboard, calendar, leaderboard, streaks, statistics, profiles, About the
Creator, read-only admin control room). **Bot and website run in one process and share one SQLite database.**
Python 3.12 · python-telegram-bot 22 · SQLite or Turso (libSQL) · Pillow · openpyxl. All dates use **Asia/Kolkata**.

> Hosting on Render? → **`DEPLOY-RENDER.md`**. Before running the real challenge read **`PERSISTENCE.md`** (the free plan erases data on restart).
> Other documents: `FINAL_CHECKLIST.md` (what was verified), `KNOWN_LIMITATIONS.md`, `CHANGES.md`.

## 1. Project structure
```
run.py                 entry point:  python run.py   (bot + website; RUN_MODE=web for website only)
winter_arc_bot.py      old launcher name, still works (calls run.py)
config.py              environment variables, validation, clock helpers, CREATOR credit (single place), deploy warnings
bot.py                 Telegram application wiring, logging with token redaction, startup message to admins
database.py            SQLite schema, versioned migrations (backup first), transactions, audit log, confirmation tokens
scoring.py             the only place with point / rounding / status / streak rules (pure functions)
reports.py             targets, reports, edit history, penalties, statistics (single source of truth)
participants.py        register / edit / enroll / deactivate (history is never deleted)
participant.py         participant menu and step-by-step forms        admin.py   button admin panel + admin commands
leaderboard.py chart.py dashboard.py imaging.py export.py   rankings, images, XLSX
scheduler.py           reminders + nightly backup (03:00 IST, newest 14 kept)
tg.py utils.py         small helpers
dbconn.py              Turso (libSQL) compatibility layer: shared locked connection, reconnects, read-only view, snapshots, restore
web.py                 website: routes, pages, read-only database access, admin login
web_theme.py           page frame (header, opening scene, footer), shared building blocks
web_doctor.py          "Dr. Arc" study-doctor character (one SVG component, 9 states)
web_charts.py          dependency-free SVG bar/line charts
web_theme_base.py      HTML escaping helper
web_static/            app.css  app.js  boot.js  arc-latin.woff   (served at /static/…, nothing from outside)
fonts/                 DejaVu fonts used for chart images
tools/                 turso_check.py (connection self-test) · restore_backup.py · make_creator_font.py
tests/                 Python tests (pytest) + tests/browser/ (real-browser regression tests, see §7)
```

## 2. Run locally
```bash
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt                     # requirements.txt = same without the test tools
cp .env.example .env                                    # then edit .env
python run.py                                           # bot + website → http://localhost:8080
```
`RUN_MODE=web python run.py` starts only the website (no Telegram token needed).
**BotFather:** `/newbot` → copy the token into `BOT_TOKEN`. Participants must open a **private chat** with the bot and press *Start* (Telegram does not let bots message people first).
**Your numeric Telegram ID:** send `/myid` to the bot (or use @userinfobot) and put admin IDs in `ADMIN_IDS`. Admin rights come **only** from that variable and are checked on the server for every admin button and command.

## 3. Environment variables
| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | – | Required unless `RUN_MODE=web`. From @BotFather. Never logged. |
| `ADMIN_IDS` | empty | Comma-separated numeric Telegram IDs. Empty = nobody is admin (a warning is shown). |
| `WEB_ADMIN_PASSWORD` | empty | Password for the website's Admin page (use 8+ characters). Empty = admin page switched off. |
| `WEB_SECRET` | random per start | Signs the admin login cookie. Set it so logins survive restarts. |
| `DB_PATH` | `arc.db` (project folder) | SQLite file. |
| `BACKUP_DIR` | `backups` | Where automatic/migration backups go. |
| `RUN_MODE` | `all` | `all` = bot + website · `bot` · `web`. |
| `PORT` | `8080` | Website port (Render sets this itself). |
| `LOG_LEVEL` | `INFO` | |
| `FONT_PATH` | – | Optional Unicode font for chart images. |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | – | Use the external Turso database (set BOTH or neither). `TURSO-SETUP.md` |
| `BACKUP_CHAT_ID` | – | Private Telegram group for off-site backups. `BACKUP-RESTORE.md` |
| `BACKUP_EVERY_HOURS`, `BACKUP_KEEP_DAYS` | `24`, `30` | Backup interval / local copies kept |
| `PUBLIC_URL` | – | Optional website address shown in the admins' startup message (Render's `RENDER_EXTERNAL_URL` is used automatically). |

Read automatically on Render: `RENDER`, `RENDER_EXTERNAL_URL`, `PORT`. Startup problems (no admins, no password, database on a temporary disk) are listed as `SETUP:` lines in the log, in the admins' Telegram startup message and on the website's Admin page.

## 4. Daily flow
1. **Targets** (`/target`): due by **09:00 IST on the day they apply to** (can be entered the evening before, edited until 09:00; every version is kept).
2. **Report** (`/report`): today or yesterday (configurable window). Durations like `7:30`, `7h30m`, `450m` — a bare `8` is rejected because it is ambiguous. The participant sees **provisional** points.
3. **Approval:** admin panel → *Pending reports* → Approve / Reject / Request correction (reason required). Only **approved** reports count anywhere.
4. Participants can resubmit while *pending / rejected / correction requested*. Approved reports are changed only by admins (*Correct a report*); points are **recomputed, never added again**.

## 5. Rules implemented (configurable in `/admin → Challenge settings`)
* **Study hours** — policy `tolerance_up` (default): whole hours count; a leftover within 10 min of the next hour counts as that hour (7h50 → 8, 8h09 → 8, 8h50 → 9). The *same* tolerance decides full-green (7h50 = full green). `floor` = no tolerance. Study time = independent + school minutes.
* Exercise: 1 point per session, max 2/day. Subjects: 1 point each for ≥15 Physics / Chemistry / Biology questions (max 3).
* **Net = gross − active penalties.** Points are never stored; they are derived from approved reports, so edits can't double count.
* **Penalties** (ledger, one active penalty per participant per day, admin confirmation, reversible with audit): wasted −5, partial −2, health −3 *only if the health-penalty policy is switched on* (default **off**; health days are a separate HEALTH status without deduction). The partial-day −2 is created when a partial report is **approved**, removed if a correction makes it a full day, never duplicated by edits, never re-applied after an admin reversed it.
* **Statuses:** GREEN, HALF, RED, EMPTY, PENDING, HEALTH. Admin override needs a reason and is audited; statuses never change points.
* **Streak** = consecutive GREEN days. Future days are ignored; today never breaks a streak while EMPTY/PENDING; a HEALTH, HALF, RED or missing past day ends it.
* **Average study** = verified minutes ÷ elapsed days. **Leaderboard ties** → net, hours, longest streak, name, ID.

## 6. Telegram commands
Participants: `/start /menu /register /target /report /profile /points /history /leaderboard (/board) /rules /rename /chart /dashboard /myid /cancel`
Admins (private chat): `/admin` plus `/addp /import /editp /link /logreport /penalty /status /newsession /sessions /activate /export /backup /audit` (`/backup group` sends a backup to the backup group now; `/chatid` shows a chat's ID, admins only, works in groups).
Sessions: `/newsession 3 2026-12-01 2026-12-31 copy` creates Session 3 (rules copied, members enrolled with `copy`); older sessions stay in the database and can be viewed on the website with `?s=<session id>`.

## 7. Website
| Route | Page |
|---|---|
| `/` | Dashboard: session header, key numbers, today, top three, shortcuts (first visit: opening scene) |
| `/calendar?p=<id>` | Monthly attendance per participant (tap a day for details) + team overview |
| `/leaderboard?kind=net\|hours\|streak\|days\|avg&page=n` | Rankings incl. today's target progress |
| `/streaks`, `/stats` | Streaks and streak history · statistics and charts (daily, weekly, points over time, consistency, progress) |
| `/participants`, `/participant/<id>` | Everyone · profile with charts, calendar and daily points |
| `/about`, `/settings` | About the Creator · theme, opening scene, motion preferences, Dr. Arc gallery |
| `/admin/login`, `/admin`, `/admin/export.xlsx`, `/admin/logout` | Read-only control room (password) — approvals, penalties and corrections stay in Telegram |
| `/healthz`, `/static/<file>` | Health check (used by Render) · allow-listed assets |

Any page accepts `?s=<session id>`. **Design:** light theme by default with an optional dark theme, remembered per browser; opening scene "The Study Sanctuary" (pull the cord; Skip / Enter buttons; first visit only; replay in Settings); animations respect the device's reduced-motion setting. **Creator credit:** edit `CREATOR` in `config.py` (the only place). **Security:** the website opens the database with `PRAGMA query_only` (cannot change records), escapes all output, sends a strict Content-Security-Policy (scripts only from this site), rate-limits admin login (5 tries / 5 min per address), uses `HttpOnly`/`SameSite=Strict` (+`Secure` behind HTTPS) cookies and never shows notes or Telegram IDs publicly.

## 8. Database safety & backups
* The schema version is stored in `PRAGMA user_version`; on start the bot migrates automatically and **copies the database to `BACKUP_DIR` first**. The old single-session tables are converted in place and kept as `legacy_p` / `legacy_l`. Nothing in this project resets or deletes the database.
* Backups: `/backup` sends the database file to the admin in Telegram; a nightly copy (03:00 IST, newest 14) is written to `BACKUP_DIR`; the website's Admin page and `/export` give an XLSX export.
* Restore: stop the app, put a backup file in place of `DB_PATH`, start the app (needs shell/disk access; there is no restore command yet).
* **Where the database lives matters** — see `PERSISTENCE.md`. With `TURSO_*` set, bot, website, admin panel and scheduler all use ONE shared Turso connection (`dbconn.py`); without it the local SQLite file is used exactly as before. If Turso is configured but unreachable the app stops instead of creating a local database.
* Off-site backups: a compressed snapshot goes to the private backup group when due (default every 24 h, catching up after a sleep). Restoring: `BACKUP-RESTORE.md`.

## 9. Tests
```bash
pytest -q                          # Python: scoring, reports, penalties, streaks, migrations, access control, reminders,
                                   # bot handlers (Telegram network stubbed), website routes/security/design, bot + website together
```
Real-browser regression tests (headless Chromium) live in `tests/browser/` — see its README. Results of the final run are in `FINAL_CHECKLIST.md`.
