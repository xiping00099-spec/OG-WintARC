# Database persistence: risks and options

> **Update:** the Turso integration is implemented (see `TURSO-SETUP.md`). It is tested against a LOCAL libSQL file only; real Turso connectivity has not been verified until you run `tools/turso_check.py`. Until `TURSO_*` is set on Render, everything below still applies.

## What was observed (Render logs of your current free service)
* The database file sits inside the application folder (`/opt/render/project/src/arc.db`).
* The log line "Database migrated to schema v1" appeared at **every** start (three starts within about 1.5 hours). That line only appears when the database is created from scratch, so **the database was empty each time**: the data did not survive.
* The service stopped after roughly 20 idle minutes several times (the free plan's sleep).

## Why this matters
| Event | Free plan result |
|---|---|
| Restart, sleep → wake-up, crash recovery | Database erased (new empty file) |
| Redeploy (any GitHub upload with Auto-Deploy on) | Database erased |
| Service asleep | Bot offline; reminders and the nightly backup do not run |
| Nightly backup (03:00 IST) | Written next to the database, so it is **erased together with it** |

The redesign does **not** change any of this; it only adds a warning in the logs, in the admins' Telegram startup message and on the website's Admin page.
Nothing in this project resets, overwrites or migrates a database on its own (the only automatic schema step is the versioned migration, which copies the file first).

## What protects your records today
* `/backup` — the bot sends the full database file to you in Telegram (a copy outside Render).
* `/export` and the website Admin page — XLSX export of everything.
* Both are manual. After each study day they are the only thing between a restart and lost records.

## What does not exist yet
* Restoring is done with `tools/restore_backup.py` on your computer (there is no in-chat restore command).

## Options (decision needed before real use; none has been started)
1. **Paid Render instance + Disk** — mount a disk (e.g. `/var/data`) and set `DB_PATH=/var/data/arc.db`, `BACKUP_DIR=/var/data/backups`. No code change. Removes sleep and data loss on restart. Costs money (see Render's current prices).
2. **Free plan + Turso (implemented)** — data lives in Turso, with automatic off-site backups to a Telegram group and a restore tool. Not yet verified against real Turso.
3. **Free plan as it is** — acceptable only for testing; export after every study day.

The challenge's first day is 1 November 2026. If real attendance is being recorded on the free plan, send `/backup` and `/export` to yourself after every day.
