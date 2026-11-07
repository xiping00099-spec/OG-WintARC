# Off-site backups and restoring

## How backups work
* The app sends a compressed snapshot (`winter-arc-backup-YYYYMMDD-HHMM-IST.db.gz`) to a **private Telegram backup group** (or to the admins' chats if no group is set).
* A backup is due when the last successful one is older than `BACKUP_EVERY_HOURS` (default 24). Because Render Free sleeps, a missed backup is made as soon as the service wakes. A failed send is retried after 30 minutes. Empty databases are never sent.
* **Retention:** the Telegram group keeps every file until you delete it (a month of daily files = 30+ copies); the server keeps the newest `BACKUP_KEEP_DAYS` (30) copies locally (temporary on Render Free). Turso's free plan also offers a short point-in-time restore (check turso.tech for the current period).
* Each file's caption lists participants/reports/targets/penalties counts and a checksum.

## Create the backup group (5 minutes)
1. Telegram → **New Group** → name it "WINTER ARC Backups". Add **@OG_WintArcbot** (and a co-admin if you want). Do **not** add participants.
2. In the group, send `/chatid` (only admins in `ADMIN_IDS` get an answer; if the bot ignores it send `/chatid@OG_WintArcbot`). It replies with a number like `-1001234567890`.
3. Render → Environment → `BACKUP_CHAT_ID` = that number → save (redeploys).
4. In a private chat with the bot send `/backup group`. A `.db.gz` file must appear in the group. If it says it could not be delivered, the bot is not a member or the number is wrong.
5. If Telegram later upgrades the group to a "supergroup" the number changes: send `/chatid` again.

`/backup` (without `group`) still sends a plain copy to you.

## Restoring
Restoring needs a computer with this project installed (`pip install -r requirements.txt`).
1. Download the newest `.db.gz` from the backup group. **Do not unzip it.**
2. **Practice first** on a local file: `python tools/restore_backup.py FILE.db.gz --target-sqlite rehearsal.db` (touches nothing else).
3. Stop using the bot (Render → Suspend, or just avoid it).
4. Point the tool at the target database (set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` for the database to restore into) and run `python tools/restore_backup.py FILE.db.gz`. Type `RESTORE` when asked.
5. Safety rules built in: the file is checked first (integrity, WINTER ARC tables, version). A database that **already contains participants/reports/targets/penalties is refused** unless you add `--force`; with `--force` a snapshot of the current data is saved first (`--snapshot-dir`, default `./backups`). Everything is loaded in one transaction: all of it or nothing.
6. Resume the service. Check the website and `/leaderboard`.
