# Deploy WINTER ARC on Render (testing on the free plan)

**Read this first:** on Render's free plan the database lives on a temporary disk. It is **erased on every restart and every redeploy**,
and the free service **goes to sleep after about 15 minutes without website visits** (the bot is offline while asleep, so reminders and
the nightly backup do not run). The free plan is for **testing only**. Before running the real challenge read `PERSISTENCE.md`.
Render's screens change from time to time; if a label is slightly different, look for the closest one.

Nothing in this ZIP deploys by itself. You decide when to upload.

## A. Try the ZIP on your own computer first (recommended, 5 minutes)
1. Unzip the file. Install Python 3.12 from python.org.
2. Open a terminal in the unzipped folder and run:
   ```
   python -m venv venv
   venv\Scripts\activate          (Mac/Linux: source venv/bin/activate)
   pip install -r requirements.txt
   set RUN_MODE=web               (Mac/Linux: export RUN_MODE=web)
   python run.py
   ```
3. Open http://localhost:8080 — you will see the opening scene, then an empty dashboard ("No participants yet"). This creates a new empty `arc.db` in the folder. Stop with Ctrl+C.
   (To try the bot too, copy `.env.example` to `.env`, fill in your token and IDs, and run `python run.py` without `RUN_MODE`.)

## B. Put the new files on GitHub
Your repository is the one your Render service `WINTER-ARC` already deploys from.
> **Uploading triggers a redeploy** (Auto-Deploy is on). On the free plan that wipes the database — fine for testing, not for real records.
> To control the moment of deployment: Render → your service → **Settings → Build & Deploy → Auto-Deploy → Off**, upload to GitHub, then press **Manual Deploy → Deploy latest commit** when ready.

1. Unzip. Open the folder `winter_arc` (the one containing `run.py`).
2. On github.com open your repository → **Add file → Upload files**.
3. Drag **everything inside** the folder (all files **and** the folders `web_static`, `fonts`, `tests`, `tools`) into the page. Files with the same name are replaced; new ones are added.
   Hidden files such as `.gitignore`, `.python-version` and `.env.example` must be included (Mac: press Cmd+Shift+. to show them).
4. **Never upload** `.env`, any `*.db` file, or a `venv` folder.
5. Old files from the previous release that no longer exist (`railway.json`, `Procfile`, `DEPLOY.md`) can stay or be deleted — they are not used. (GitHub: open the file → trash-can icon → Commit.)
6. Write a commit message such as "Premium redesign" and press **Commit changes**.

## C. Check the Render service settings (once)
Render → your service:
| Setting | Value |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python run.py` |
| Health Check Path (Settings) | `/healthz` |
| Plan | Free (testing) |

**Environment** tab (keep what you already have; the redesign adds no new required variables):
| Name | Value |
|---|---|
| `BOT_TOKEN` | your token from @BotFather (type it here only; never in chat or GitHub) |
| `ADMIN_IDS` | numeric IDs, comma separated — every admin must have pressed **Start** in the bot |
| `WEB_ADMIN_PASSWORD` | 8+ characters; enables the website's Admin page |
| `WEB_SECRET` | any long random text (keeps website logins valid across restarts) |
| `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` | **Recommended.** Keeps your data across restarts. Follow `TURSO-SETUP.md` first (run `tools/turso_check.py`), then add both together |
| `BACKUP_CHAT_ID` | Private backup group's ID — `BACKUP-RESTORE.md` |

The build installs one extra package (`libsql`, listed in `requirements.txt`). If the build fails, the previous deploy keeps running; send me the build log.

## D. After the deploy: check it works
1. Render → **Logs**. Healthy lines: `Database ready (schema v1)`, `Website listening on port 10000`, `Reminder scheduler started`, `Bot started as @…`.
   A line starting `SETUP:` tells you what still needs fixing (on the free plan you will see the "database is ERASED on every restart" warning — that is expected).
2. Telegram: each admin who pressed Start receives "Winter Arc bot is online" (with the same warnings).
3. Open your service address (`https://<your-service>.onrender.com`): opening scene → dashboard. Try Calendar, Leaderboard, Statistics, About the Creator, Settings (theme, replay intro). The first visit after a sleep takes a while to wake the service.
4. In Telegram: `/start` → Register → `/report` → `/admin` → approve. Then reload the website: the numbers appear.
5. Website **Admin**: log in with `WEB_ADMIN_PASSWORD` (read-only overview, XLSX export).

## E. Going from testing to the real challenge
**With Turso configured (see `TURSO-SETUP.md`) your records survive restarts, sleeps and redeploys; Render Free still sleeps, so the bot is offline while asleep.**
Do **not** rely on the free plan for real attendance records. Pick one (details in `PERSISTENCE.md`):
* **Paid instance + Disk** (simplest, no code change): Render → service → change the instance type to a paid one → **Disks → Add Disk**, mount path `/var/data`, then add environment variables `DB_PATH=/var/data/arc.db` and `BACKUP_DIR=/var/data/backups`, redeploy. The log must then say `Database ready … /var/data/arc.db` and the "ERASED" warning must disappear.
* **Stay free** only if you accept the risk and send `/backup` to yourself (and use `/export`) after every study day.

## F. Rollback
Keep the previous release ZIP (`winter_arc_bot_project.zip`). Upload its files over the repository the same way, or in Render open **Events/Deploys** and redeploy an earlier deploy. The database is not touched by either action (on the free plan it is erased by any redeploy anyway).

## G. Problems
| Symptom | What to do |
|---|---|
| Build fails | Open the build log; the project needs only the packages in `requirements.txt` (Python 3.12 is set in `.python-version`) |
| `BOT_TOKEN is missing` | Re-enter `BOT_TOKEN` in Environment exactly as BotFather gave it |
| Bot silent, website works | Bot sleeping (free plan) or another copy of the bot is running with the same token — keep one instance |
| `/admin` says admins only | Your ID is not in `ADMIN_IDS` (send `/myid`) |
| "Chat not found" in the logs | That admin has not pressed Start in the bot (or the ID is wrong) |
| Website Admin says switched off | Set `WEB_ADMIN_PASSWORD` |
| Data vanished | Free plan restart/redeploy — see `PERSISTENCE.md` |
