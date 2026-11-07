"""bot.py - entry point. Run with:  python bot.py"""
import logging
import warnings

from telegram import BotCommand, Update
from telegram.ext import Application
from telegram.warnings import PTBUserWarning

import admin
import config
import database as db
import dbconn
import participant
import scheduler
import tg

log = logging.getLogger("winter_arc")


class RedactingFormatter(logging.Formatter):
    """Make sure the bot token can never appear in log output, even inside tracebacks or URLs."""

    def __init__(self, secret, *a, **kw):
        super().__init__(*a, **kw)
        self.secrets = [s for s in ([secret] if isinstance(secret, str) else list(secret)) if s]  # bot token, Turso token...

    def format(self, record):
        text = super().format(record)
        for s in self.secrets:
            text = text.replace(s, "***SECRET***")
        return text


def setup_logging(cfg):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter([cfg.bot_token, cfg.turso_token], "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=cfg.log_level, handlers=[handler], force=True)
    for noisy in ("httpx", "httpcore", "apscheduler"):  # httpx logs full URLs (which contain the token)
        logging.getLogger(noisy).setLevel(logging.WARNING)


COMMANDS = [("start", "Open the menu"), ("menu", "Main menu"), ("register", "Join the challenge"), ("target", "Submit daily targets"),
            ("report", "Submit daily report"), ("profile", "My profile"), ("points", "My daily points"), ("history", "My history"),
            ("leaderboard", "Leaderboard"), ("rules", "Challenge rules"), ("chart", "Attendance chart"), ("dashboard", "Statistics dashboard"),
            ("myid", "Show my Telegram ID"), ("cancel", "Cancel current action")]


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)
    cfg = app.bot_data["cfg"]
    lines = ["✅ <b>Winter Arc bot is online.</b>"] + [f"⚠️ {w}" for w in config.deploy_warnings(cfg)]
    lines.append(f"🗄 Database: {db.describe_backend()}")
    if cfg.public_url:
        lines.append(f"🌐 Website: {cfg.public_url}")
    for admin_id in sorted(cfg.admin_ids):  # tells you the deploy worked (and what still needs fixing)
        await tg.safe_send(app.bot, admin_id, "\n".join(lines))


async def post_shutdown(app: Application):
    conn = app.bot_data.get("conn")
    if conn is not None:
        getattr(conn, "shutdown", conn.close)()
        log.info("Database closed. Goodbye.")


async def on_error(update, context):
    """Log the problem (token-redacted) and give the user a calm message instead of silence."""
    unavailable = isinstance(context.error, dbconn.DatabaseUnavailable)
    if unavailable:
        log.warning("Database unavailable while handling an update: %s", context.error)
    else:
        log.error("Unhandled error", exc_info=context.error)
    text = ("⚠️ The database is temporarily unavailable, so your last action may NOT have been saved. "
            "Please try again in a minute — repeating it is safe." if unavailable
            else "⚠️ Something went wrong on my side. Please try again in a moment.")
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(text)
        elif isinstance(update, Update) and update.callback_query:
            await tg.safe_answer(update.callback_query, text[:190], show_alert=True)
    except Exception:  # noqa: BLE001
        pass


def build_application(cfg, conn) -> Application:
    warnings.filterwarnings("ignore", category=PTBUserWarning)  # "CallbackQueryHandler with per_message=False" notice
    app = Application.builder().token(cfg.bot_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.bot_data["conn"] = conn
    app.bot_data["cfg"] = cfg
    g0, g1 = admin.build_handlers()
    for h in participant.build_handlers():  # the form conversation comes first so it gets typed answers
        app.add_handler(h, group=0)
    for h in g0:
        app.add_handler(h, group=0)
    for h in g1:
        app.add_handler(h, group=1)
    app.add_error_handler(on_error)
    scheduler.schedule(app)
    return app
