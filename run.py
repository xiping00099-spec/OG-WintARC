"""
run.py - the ONE entry point:  python run.py

RUN_MODE=all (default)  -> Telegram bot + website in one process, one shared database
RUN_MODE=bot            -> bot only
RUN_MODE=web            -> website only (handy for looking at data locally)
"""
import logging
import sys

from telegram import Update
from telegram.error import InvalidToken, NetworkError

import bot
import config
import database as db
import dbconn
import web

log = logging.getLogger("winter_arc")


def main():
    try:
        cfg = config.load_config()
    except config.ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    bot.setup_logging(cfg)
    for w in config.deploy_warnings(cfg):
        log.warning("SETUP: %s", w)
    if cfg.uses_turso:
        db.configure_backend(cfg.turso_url, cfg.turso_token)  # bot, website, admin panel and scheduler all share this one connection
    else:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = db.open_database(cfg.db_path, cfg.backup_dir)  # waits for Turso (~35 s), then migrations (with backup) run BEFORE anything else
    except dbconn.DatabaseConfigError as e:
        log.error("Database credentials rejected: %s", e)
        sys.exit(6)
    except dbconn.DatabaseUnavailable as e:
        log.error("Turso is configured but not reachable (%s). Not falling back to a local file; stopping so Render restarts the service.", e)
        sys.exit(5)
    log.info("Database ready (schema v%s) · %s", db.schema_version(conn), db.describe_backend() if cfg.uses_turso else cfg.db_path)

    if cfg.run_mode == "web":
        conn.close()
        return web.serve_forever(cfg)
    if cfg.run_mode == "all":
        try:
            web.start_in_background(cfg)
        except OSError as e:  # the bot is more important than the website
            log.error("Website could not start on port %s: %s", cfg.port, e)
    try:
        bot.build_application(cfg, conn).run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except InvalidToken:
        log.error("Telegram rejected the bot token. Copy BOT_TOKEN again from @BotFather.")
        sys.exit(3)
    except NetworkError as e:
        log.error("Could not reach Telegram (%s).", type(e).__name__)
        sys.exit(4)  # non-zero exit => Render restarts the service automatically


if __name__ == "__main__":
    main()
