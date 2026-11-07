"""
config.py - all settings in one place.

Secrets (bot token, admin IDs) come from environment variables or a local
.env file. Challenge rules (hours, tolerance, penalties...) live in the
database `sessions` table so they can be changed without editing code.
"""
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TZ = ZoneInfo("Asia/Kolkata")  # every date/time in the bot uses this zone

# Creator credit - the ONLY place these details are written. The website footer and the
# About page read them from here, so changing them later means editing these three lines.
CREATOR = {
    "name": "Aʀcᴀɴᴇ ( OG ASSOCIATION )",
    "telegram_username": "TalkToArcane",
    "project": "WINTER ARC",
}


def creator_telegram_url() -> str:
    return f"https://t.me/{CREATOR['telegram_username']}"

# Used ONLY to create the very first session in a brand-new database.
# After that, change rules from the admin panel (/admin -> Challenge settings).
DEFAULT_SESSION = {
    "name": "WINTER ARC",
    "session_number": 2,
    "group_name": "THE OG ASSOCIATION",
    "start_date": "2026-11-01",
    "end_date": "2026-11-30",
    "full_green_minutes": 8 * 60,
    "tolerance_minutes": 10,
    "target_deadline": "09:00",
}

# Reminder defaults (stored in the bot_settings table; editable by admins).
DEFAULT_BOT_SETTINGS = {
    "rem_morning_enabled": "1",
    "rem_morning_time": "08:00",
    "rem_evening_enabled": "1",
    "rem_evening_time": "21:00",
    "rem_correction_enabled": "1",
    "rem_correction_time": "18:00",
    "rem_admin_enabled": "1",
    "rem_admin_time": "20:00",
    "chart_page_size": "22",
}


class ConfigError(Exception):
    """Raised when the environment is not set up correctly."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset
    db_path: Path
    backup_dir: Path
    log_level: str
    font_path: str
    run_mode: str = "all"            # all = bot + web, bot = bot only, web = web only
    port: int = 8080
    web_admin_password: str = ""
    web_secret: str = ""
    public_url: str = ""
    on_render: bool = False
    turso_url: str = ""              # both TURSO_* set => the external Turso database is used
    turso_token: str = ""
    backup_chat_id: int = 0          # private Telegram group that receives off-site backups (0 = not set)
    backup_every_hours: int = 24
    backup_keep: int = 30            # local backup copies kept (days)

    @property
    def uses_turso(self) -> bool:
        return bool(self.turso_url and self.turso_token)


def _parse_admin_ids(raw: str) -> frozenset:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"ADMIN_IDS contains a non-numeric value: {part!r}")
        ids.add(int(part))
    return frozenset(ids)


def load_config(require_token=None) -> Config:
    """Read settings from environment variables / .env and validate them."""
    env = os.environ.get
    mode = env("RUN_MODE", "all").strip().lower() or "all"
    if mode not in ("all", "bot", "web"):
        raise ConfigError("RUN_MODE must be all, bot or web.")
    if require_token is None:
        require_token = mode in ("all", "bot")
    token = env("BOT_TOKEN", "").strip()
    if require_token and (not token or ":" not in token):
        raise ConfigError("BOT_TOKEN is missing or malformed. Copy it again from @BotFather.")
    admin_ids = _parse_admin_ids(env("ADMIN_IDS", ""))
    default_db, default_bk = "arc.db", "backups"  # relative to the project folder; on a host with a disk set DB_PATH / BACKUP_DIR
    db_path, backup_dir = Path(env("DB_PATH", default_db)), Path(env("BACKUP_DIR", default_bk))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    if not backup_dir.is_absolute():
        backup_dir = BASE_DIR / backup_dir
    level = env("LOG_LEVEL", "INFO").upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(f"LOG_LEVEL {level!r} is not valid.")
    port_text = env("PORT", "8080").strip()
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ConfigError("PORT must be a number between 1 and 65535.")
    render_url = env("RENDER_EXTERNAL_URL", "").strip()
    turso_url, turso_token = env("TURSO_DATABASE_URL", "").strip(), env("TURSO_AUTH_TOKEN", "").strip()
    if bool(turso_url) != bool(turso_token):
        raise ConfigError("Set BOTH TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (or neither). Refusing to start with half a database setup.")
    if turso_url and not turso_url.startswith(("libsql://", "https://", "wss://", "ws://", "http://")):
        raise ConfigError("TURSO_DATABASE_URL must look like libsql://your-database-yourname.turso.io")
    chat_text = env("BACKUP_CHAT_ID", "").strip()
    if chat_text and not re.fullmatch(r"-?\d{5,20}", chat_text):
        raise ConfigError("BACKUP_CHAT_ID must be the numeric chat ID of your backup group (usually starts with -100).")
    hours_text, keep_text = env("BACKUP_EVERY_HOURS", "24").strip(), env("BACKUP_KEEP_DAYS", "30").strip()
    if not hours_text.isdigit() or not 1 <= int(hours_text) <= 168 or not keep_text.isdigit() or not 1 <= int(keep_text) <= 365:
        raise ConfigError("BACKUP_EVERY_HOURS must be 1-168 and BACKUP_KEEP_DAYS must be 1-365.")
    return Config(token, admin_ids, db_path, backup_dir, level, env("FONT_PATH", ""), mode, int(port_text),
                  env("WEB_ADMIN_PASSWORD", ""), env("WEB_SECRET", ""),
                  render_url or env("PUBLIC_URL", "").strip(),
                  env("RENDER", "").strip().lower() == "true",
                  turso_url, turso_token, int(chat_text) if chat_text else 0, int(hours_text), int(keep_text))


def deploy_warnings(cfg: Config) -> list:
    """Plain-language problems an admin should fix. Shown in the logs, in Telegram and on the web admin page."""
    out = []
    if cfg.run_mode in ("all", "bot") and not cfg.admin_ids:
        out.append("ADMIN_IDS is empty, so nobody can use admin features. Send /myid to the bot, put the number in ADMIN_IDS and redeploy.")
    if cfg.on_render and str(cfg.db_path).replace("\\", "/").startswith(("/opt/render/project", "C:/opt/render/project")):
        out.append("Render free hosting: the database is stored on a temporary disk and is ERASED on every restart or redeploy, and "
                   "the free service sleeps after ~15 minutes without web visits (the bot is offline while asleep). "
                   "Connect Turso (see TURSO-SETUP.md) to keep the data, or use this for testing only and send /backup often.")
    if cfg.uses_turso and not cfg.backup_chat_id:
        out.append("BACKUP_CHAT_ID is not set: off-site backups go to the admins' private chats. A dedicated private backup group is recommended (see BACKUP-RESTORE.md).")
    if cfg.run_mode in ("all", "web") and not cfg.web_admin_password:
        out.append("WEB_ADMIN_PASSWORD is not set, so the web admin page is switched off.")
    elif cfg.web_admin_password and len(cfg.web_admin_password) < 8:
        out.append("WEB_ADMIN_PASSWORD is shorter than 8 characters. Please use a longer one.")
    return out


# ---- clock helpers (tests can replace `now_func` to fake the time) ----
now_func = lambda: datetime.now(TZ)  # noqa: E731


def now() -> datetime:
    return now_func()


def today() -> date:
    return now().date()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")
