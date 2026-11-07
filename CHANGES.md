# Release notes

## Turso integration release (on top of the premium redesign)
**New:** `dbconn.py`, `tools/turso_check.py`, `tools/restore_backup.py`, `tests/test_dbconn.py`, `TURSO-SETUP.md`, `BACKUP-RESTORE.md`.
**Changed:** `database.py` (backend selection, locked transactions, snapshots, schema-version fallback), `web.py` (shared read-only connection, 503 page, healthz, Admin storage panel), `config.py`, `run.py`, `bot.py`, `participant.py` (honest failed-save messages + Try again), `admin.py` (`/chatid`, `/backup group`), `scheduler.py` (off-site backups), `tg.py` ("Query is too old" fix), `utils.py`, `requirements.txt` (+libsql), `.env.example`, docs, `tests/conftest.py`.
**Unchanged:** schema, scoring, penalties, streaks, permissions, all page designs, bot identity and command menu.

# Premium redesign release

## Unchanged (byte-for-byte identical to the previous release)
`admin.py bot.py participant.py participants.py scoring.py reports.py database.py scheduler.py leaderboard.py chart.py dashboard.py
export.py imaging.py tg.py utils.py run.py` — the Telegram bot, scoring, penalties, streaks, permissions, reminders, backups and the database schema.
(`run.py` only had a comment changed.) No migration, reset or schema change.

## Changed
* `web.py` — all pages redesigned; new routes `/participants`, `/about`, `/settings`, `/static/…`; stricter Content-Security-Policy; gzip for static files.
* `web_theme.py` — new page frame (header, navigation, opening scene, footer) and building blocks.
* `config.py` — creator credit in one place (`CREATOR`), Render detection (`RENDER`, `RENDER_EXTERNAL_URL`), warning when the database is on Render's temporary disk; old Railway detection removed.
* `README.md`, `.env.example` — rewritten for the current code and Render.
* `tests/test_web.py` — updated for the new markup; Railway tests replaced by Render ones.

## New
`web_static/app.css, app.js, boot.js, arc-latin.woff` · `web_doctor.py` (study doctor) · `web_charts.py` (SVG charts) · `web_theme_base.py` ·
`tools/make_creator_font.py` · `tests/test_web_design.py` · `tests/browser/` (real-browser tests) ·
`DEPLOY-RENDER.md, PERSISTENCE.md, KNOWN_LIMITATIONS.md, CHANGES.md, FINAL_CHECKLIST.md`.

## Removed
`railway.json`, `Procfile`, `DEPLOY.md` (old hosting) and the Railway code in `config.py`.

## Design summary
Apple-inspired light theme (optional dark) with a restrained electric-lime accent; "The Study Sanctuary" lamp opening scene; Dr. Arc the study doctor
(9 states); redesigned Home, Calendar (interactive day details), Leaderboard (with today's target progress), Streaks, Statistics (daily, weekly,
points-over-time, streak-history, consistency, progress), Participants, Profiles, About the Creator, Settings, Admin; responsive mobile menu;
scroll reveals, counters, tooltips, confirmation dialogs, toasts, page transitions; keyboard, focus and reduced-motion support.
