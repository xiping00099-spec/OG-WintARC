"""
scheduler.py - automatic reminders using python-telegram-bot's JobQueue.

One job runs every minute (`tick`). It looks at the reminder settings stored in the database
(bot_settings) and the Asia/Kolkata clock. Every reminder is first "claimed" in the table
reminders_sent (primary key = kind+date+session+recipient), so the same reminder is never sent twice,
even after a restart. People who never started the bot (Telegram refuses) are marked 'blocked',
skipped quietly and shown to admins in the participant profile.
"""
import asyncio
import hashlib
import logging
from pathlib import Path
from datetime import datetime, time as dtime, timedelta

import config
import database as db
import dbconn
import participants as ppl
import reports
import tg
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

log = logging.getLogger(__name__)
CATCH_UP_MINUTES = 120  # after a restart, a reminder is still sent if we are at most this late


def due(now: datetime, hhmm: str) -> bool:
    hh, mm = (int(x) for x in hhmm.split(":"))
    at = datetime.combine(now.date(), dtime(hh, mm), tzinfo=config.TZ)
    return timedelta(0) <= now - at <= timedelta(minutes=CATCH_UP_MINUTES)


def claim(conn, key, participant_id) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO reminders_sent(key, participant_id, status, created_at) VALUES(?,?,'claimed',?)",
                       (key, participant_id, config.now_iso()))
    return cur.rowcount == 1


async def _send_once(conn, bot, key, participant_id, chat_id, text):
    if not claim(conn, key, participant_id):
        return None
    status = await tg.safe_send(bot, chat_id, text)
    conn.execute("UPDATE reminders_sent SET status=? WHERE key=?", (status, key))
    await asyncio.sleep(0.05)  # stay far below Telegram's flood limits
    return status


async def run_due(conn, bot, admin_ids, now=None):
    """Send every reminder that is due right now. Returns {kind: number_sent} (used by tests)."""
    now = now or config.now()
    session = db.get_active_session(conn)
    result = {}
    if session is None:
        return result
    start, end = reports.session_range(session)
    today = now.date()
    if not start <= today <= end:
        return result
    sid = session["id"]
    members = [m for m in ppl.list_members(conn, sid) if m["telegram_user_id"]]
    for kind in ("morning", "evening", "correction", "admin"):
        if db.get_setting(conn, f"rem_{kind}_enabled", "1") != "1" or not due(now, db.get_setting(conn, f"rem_{kind}_time", "08:00")):
            continue
        sent = 0
        if kind == "admin":
            n = len(reports.pending_reports(conn, sid))
            if n:
                for aid in sorted(admin_ids):
                    if await _send_once(conn, bot, f"admin:{today}:{sid}:{aid}", None, aid,
                                        f"📥 <b>{n}</b> report(s) are waiting for approval. Open /admin → Pending reports.") == "sent":
                        sent += 1
        else:
            for m in members:
                text = None
                if kind == "morning":
                    if now <= reports.target_deadline(session, today) and reports.get_target(conn, m["id"], sid, today) is None:
                        text = f"🎯 Good morning! Set today's targets before {session['target_deadline']} IST: /target"
                elif kind == "evening":
                    if reports.get_report(conn, m["id"], sid, today) is None:
                        text = "📝 Evening check-in: you haven't submitted today's report yet. Send /report"
                elif kind == "correction":
                    n = conn.execute("SELECT COUNT(*) FROM daily_reports WHERE participant_id=? AND session_id=? "
                                     "AND status='correction_requested'", (m["id"], sid)).fetchone()[0]
                    if n:
                        text = f"✏️ An admin asked you to correct {n} report(s). Please resubmit with /report."
                if text and await _send_once(conn, bot, f"{kind}:{today}:{sid}:{m['id']}", m["id"], m["telegram_user_id"], text) == "sent":
                    sent += 1
        result[kind] = sent
    return result


async def tick(context):
    """JobQueue callback. Never raises: a failing reminder must not stop the bot."""
    try:
        await run_due(tg.conn_of(context), context.bot, tg.cfg_of(context).admin_ids)
    except Exception:  # noqa: BLE001
        log.exception("Reminder tick failed")


async def housekeeping(context):
    try:
        db.purge_old_pending_actions(tg.conn_of(context))
    except Exception:  # noqa: BLE001
        log.exception("Housekeeping failed")


def prune_backups(backup_dir, keep=14, pattern="arc-daily-*.db") -> int:
    """Keep only the newest `keep` automatic backups matching `pattern` (manual / pre-migration ones are never deleted)."""
    files = sorted(Path(backup_dir).glob(pattern), key=lambda p: p.name)
    old = files[:-keep] if keep > 0 else files
    for f in old:
        f.unlink(missing_ok=True)
    return len(old)


OFFSITE_OK, OFFSITE_ERR, OFFSITE_TRY = "offsite_backup_last_ok", "offsite_backup_last_error", "offsite_backup_last_try"
RETRY_MINUTES = 30   # after a failed attempt, wait this long before trying again (a wrong group ID must not cause a flood)


def _ts(text):
    return datetime.fromisoformat(text) if text else None


async def _send_file(bot, chat_id, path, caption) -> str:
    """'sent', 'blocked' (bot not in that chat / never started) or 'error'."""
    for _ in range(2):
        try:
            with open(path, "rb") as fh:
                await bot.send_document(chat_id, document=fh, filename=Path(path).name, caption=caption)
            return "sent"
        except Forbidden:
            return "blocked"
        except RetryAfter as e:
            await asyncio.sleep(min(float(e.retry_after) + 1, 30))
        except BadRequest as e:
            log.warning("backup send failed (%s): %s", chat_id, e)
            return "blocked" if "chat not found" in str(e).lower() else "error"
        except TelegramError as e:
            log.warning("backup send failed (%s): %s", chat_id, e)
            return "error"
    return "error"


def _counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("participants", "daily_reports", "daily_targets", "penalties")}


async def run_offsite_backup(conn, bot, cfg, now=None, force=False) -> dict:
    """
    Send a compressed, consistent snapshot of the database to the private backup group (BACKUP_CHAT_ID) - or to the admins' chats if
    no group is set. Works for the local file and for Turso. Rules:
      * due when the last successful backup is older than BACKUP_EVERY_HOURS - so after a sleep it simply catches up on wake-up
      * a failed attempt is retried after 30 minutes; the last success time only moves when at least one chat really received the file
      * an empty database (nothing registered yet / freshly erased free-plan disk) is never sent
      * the newest BACKUP_KEEP_DAYS compressed copies are kept locally; the Telegram group keeps every copy until you delete it
    """
    now = now or config.now()
    if not force:
        last_ok, last_try = _ts(db.get_setting(conn, OFFSITE_OK)), _ts(db.get_setting(conn, OFFSITE_TRY))
        if last_ok and now - last_ok < timedelta(hours=cfg.backup_every_hours):
            return {"status": "not-due"}
        if last_try and now - last_try < timedelta(minutes=RETRY_MINUTES):
            return {"status": "waiting-retry"}
    counts = _counts(conn)
    if not counts["participants"] and not counts["daily_reports"]:
        return {"status": "empty-skipped"}
    targets = [cfg.backup_chat_id] if cfg.backup_chat_id else sorted(cfg.admin_ids)
    stamp = now.isoformat(timespec="seconds")
    with db.transaction(conn):
        db.set_setting(conn, OFFSITE_TRY, stamp)
    if not targets:
        with db.transaction(conn):
            db.set_setting(conn, OFFSITE_ERR, f"{stamp}: no BACKUP_CHAT_ID and no admins to send to")
        return {"status": "failed", "results": {}}
    raw = db.backup_database(conn, cfg.backup_dir, "offsite")
    gz = dbconn.gzip_file(raw, dbconn.unique_path(Path(cfg.backup_dir) / f"winter-arc-backup-{now:%Y%m%d-%H%M}-IST.db.gz"))
    raw.unlink(missing_ok=True)
    sha = hashlib.sha256(gz.read_bytes()).hexdigest()[:16]
    caption = (f"WINTER ARC backup · {now:%Y-%m-%d %H:%M} IST\n{counts['participants']} participants · {counts['daily_reports']} reports · "
               f"{counts['daily_targets']} targets · {counts['penalties']} penalties\nsha256 {sha}… · restore: BACKUP-RESTORE.md")
    results = {}
    for chat in targets:
        results[chat] = await _send_file(bot, chat, gz, caption)
        await asyncio.sleep(0.05)
    ok = any(v == "sent" for v in results.values())
    with db.transaction(conn):
        if ok:
            db.set_setting(conn, OFFSITE_OK, stamp)
            db.set_setting(conn, OFFSITE_ERR, "")
            db.audit(conn, 0, "offsite_backup", "database", gz.name, {"counts": counts, "sha256": sha})
        else:
            db.set_setting(conn, OFFSITE_ERR, f"{stamp}: not delivered ({', '.join(f'{c}: {v}' for c, v in results.items())})")
    prune_backups(cfg.backup_dir, keep=cfg.backup_keep, pattern="winter-arc-backup-*.db.gz")
    return {"status": "sent" if ok else "failed", "results": results, "file": gz.name, "counts": counts}


async def offsite_job(context):
    """Runs every 10 minutes; does real work only when a backup is due. Never raises."""
    try:
        res = await run_offsite_backup(tg.conn_of(context), context.bot, tg.cfg_of(context))
        if res["status"] in ("sent", "failed"):
            log.info("Off-site backup: %s", res["status"])
    except Exception:  # noqa: BLE001
        log.exception("Off-site backup job failed")


async def daily_backup(context):
    try:
        cfg = tg.cfg_of(context)
        db.backup_database(tg.conn_of(context), cfg.backup_dir, "daily")
        prune_backups(cfg.backup_dir, keep=cfg.backup_keep)
    except Exception:  # noqa: BLE001
        log.exception("Daily backup failed")


def schedule(application):
    jq = application.job_queue
    if jq is None:
        log.error('JobQueue unavailable: install with pip install "python-telegram-bot[job-queue]". Reminders are DISABLED.')
        return
    jq.run_repeating(tick, interval=60, first=20, name="reminders")
    jq.run_repeating(housekeeping, interval=3600, first=120, name="housekeeping")
    jq.run_repeating(offsite_job, interval=600, first=90, name="offsite-backup")
    if not application.bot_data["cfg"].uses_turso:   # with Turso the data already lives off the server; the off-site job above covers backups
        jq.run_daily(daily_backup, time=dtime(3, 0, tzinfo=config.TZ), name="daily-backup")
    log.info("Reminder scheduler started (checks every 60 s, Asia/Kolkata).")
