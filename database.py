"""
database.py - SQLite connection, versioned migrations, backups and small shared helpers
(sessions, settings, audit log, one-time confirmation tokens).

Schema versioning: `PRAGMA user_version` stores the schema number. On startup
`migrate()` runs every migration newer than the stored number, each inside a
transaction. A backup copy of the database is made before any migration of an
existing database.

Backends: by default a local SQLite file. When TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are configured (configure_backend), every
module receives the shared Turso connection from dbconn.py instead. There is NEVER an automatic fallback from Turso to a local
file: if Turso is configured but unreachable the app waits, retries and finally stops (a fallback would split the records).
"""
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import config
import dbconn
import scoring

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- connection
_REMOTE = None    # (url, token) when Turso is configured
_FACTORY = None   # tests only: callable(db_path) -> RemoteConnection over a local libSQL file


def configure_backend(url, token):
    """Switch every connection in this process to Turso."""
    global _REMOTE
    _REMOTE = (url, token)


def configure_backend_factory(factory):
    global _FACTORY
    _FACTORY = factory


def reset_backend():
    global _REMOTE, _FACTORY
    _REMOTE = _FACTORY = None
    dbconn.reset_shared()


def is_remote() -> bool:
    return bool(_REMOTE or _FACTORY)


def describe_backend() -> str:
    if _REMOTE:
        from urllib.parse import urlparse
        return f"Turso ({urlparse(_REMOTE[0]).hostname or 'remote'})"
    return "Turso (test driver)" if _FACTORY else "SQLite file"


def connect(db_path):
    if _FACTORY:
        return _FACTORY(db_path)
    if _REMOTE:
        return dbconn.shared_remote(*_REMOTE)   # ONE shared connection for bot, website and scheduler
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect_readonly(db_path):
    """Remote backends only: the shared connection wrapped so that nothing can write through it (used by the website)."""
    return dbconn.ReadOnlyView(connect(db_path))


def _quiet(conn, sql):
    """Run a cleanup statement; a dead connection must not hide the original error."""
    try:
        conn.execute(sql)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _transaction(conn):
    if conn.in_transaction:
        name = "sp_" + secrets.token_hex(4)
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            _quiet(conn, f"ROLLBACK TO {name}")
            _quiet(conn, f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            _quiet(conn, "ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


@contextmanager
def transaction(conn):
    """
    BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on error. Nested use becomes a SAVEPOINT.
    On a shared (remote) connection the connection's lock is held for the WHOLE transaction, so other threads (website, scheduler)
    wait instead of slipping statements into it. (No `await` is ever used inside a transaction block - a test enforces that.)
    """
    lock = getattr(conn, "tx_lock", None)
    if lock is None:
        with _transaction(conn) as c:
            yield c
    else:
        with lock:
            with _transaction(conn) as c:
                yield c


def backup_database(conn, backup_dir, label="manual") -> Path:
    """Consistent copy of the live database as a plain SQLite file (works for the local file and for Turso)."""
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = config.now().strftime("%Y%m%d-%H%M%S")
    target = dbconn.unique_path(backup_dir / f"arc-{label}-{stamp}.db")
    if getattr(conn, "is_remote", False):
        dbconn.dump_to_sqlite(conn, target)
    else:
        dest = sqlite3.connect(str(target))
        try:
            conn.backup(dest)
        finally:
            dest.close()
    log.info("Database backup written: %s", target.name)
    return target


# --------------------------------------------------------------------- schema v1
SCHEMA_V1 = [
    """CREATE TABLE sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        session_number INTEGER NOT NULL,
        group_name TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        full_green_minutes INTEGER NOT NULL DEFAULT 480 CHECK(full_green_minutes > 0),
        tolerance_minutes INTEGER NOT NULL DEFAULT 10 CHECK(tolerance_minutes BETWEEN 0 AND 59),
        rounding_policy TEXT NOT NULL DEFAULT 'tolerance_up' CHECK(rounding_policy IN ('floor','tolerance_up')),
        points_per_hour INTEGER NOT NULL DEFAULT 1 CHECK(points_per_hour >= 0),
        max_exercise_points INTEGER NOT NULL DEFAULT 2 CHECK(max_exercise_points >= 0),
        subject_min_questions INTEGER NOT NULL DEFAULT 15 CHECK(subject_min_questions >= 1),
        subject_point_value INTEGER NOT NULL DEFAULT 1 CHECK(subject_point_value >= 0),
        penalty_wasted INTEGER NOT NULL DEFAULT 5 CHECK(penalty_wasted >= 0),
        penalty_partial INTEGER NOT NULL DEFAULT 2 CHECK(penalty_partial >= 0),
        penalty_health INTEGER NOT NULL DEFAULT 3 CHECK(penalty_health >= 0),
        apply_partial_penalty INTEGER NOT NULL DEFAULT 1,
        health_penalty_confirmed INTEGER NOT NULL DEFAULT 0,
        target_deadline TEXT NOT NULL DEFAULT '09:00',
        report_window_days INTEGER NOT NULL DEFAULT 2 CHECK(report_window_days >= 1),
        is_active INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(name, session_number),
        CHECK(start_date <= end_date))""",
    "CREATE UNIQUE INDEX ux_one_active_session ON sessions(is_active) WHERE is_active = 1",
    """CREATE TABLE participants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_user_id INTEGER UNIQUE,
        username TEXT,
        display_name TEXT NOT NULL,
        school_type TEXT NOT NULL DEFAULT 'dummy' CHECK(school_type IN ('school','dummy')),
        registered_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        registered_by INTEGER)""",
    """CREATE TABLE session_members(
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        participant_id INTEGER NOT NULL REFERENCES participants(id),
        enrolled_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        removed_at TEXT,
        PRIMARY KEY(session_id, participant_id))""",
    """CREATE TABLE daily_targets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id INTEGER NOT NULL REFERENCES participants(id),
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        target_date TEXT NOT NULL,
        planned_minutes INTEGER NOT NULL CHECK(planned_minutes >= 0),
        planned_physics INTEGER NOT NULL DEFAULT 0 CHECK(planned_physics >= 0),
        planned_chemistry INTEGER NOT NULL DEFAULT 0 CHECK(planned_chemistry >= 0),
        planned_biology INTEGER NOT NULL DEFAULT 0 CHECK(planned_biology >= 0),
        planned_exercises INTEGER NOT NULL DEFAULT 0 CHECK(planned_exercises >= 0),
        notes TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        is_late INTEGER NOT NULL DEFAULT 0,
        submitted_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(participant_id, session_id, target_date))""",
    """CREATE TABLE target_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER NOT NULL REFERENCES daily_targets(id),
        version INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        actor_id INTEGER,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE daily_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id INTEGER NOT NULL REFERENCES participants(id),
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        report_date TEXT NOT NULL,
        independent_minutes INTEGER NOT NULL DEFAULT 0 CHECK(independent_minutes >= 0),
        school_minutes INTEGER NOT NULL DEFAULT 0 CHECK(school_minutes >= 0),
        exercise_count INTEGER NOT NULL DEFAULT 0 CHECK(exercise_count >= 0),
        exercise_names TEXT,
        physics_q INTEGER NOT NULL DEFAULT 0 CHECK(physics_q >= 0),
        chemistry_q INTEGER NOT NULL DEFAULT 0 CHECK(chemistry_q >= 0),
        biology_q INTEGER NOT NULL DEFAULT 0 CHECK(biology_q >= 0),
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('draft','pending','approved','rejected','correction_requested')),
        version INTEGER NOT NULL DEFAULT 1,
        entered_by_admin INTEGER NOT NULL DEFAULT 0,
        submitted_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        reviewed_by INTEGER,
        reviewed_at TEXT,
        review_note TEXT,
        UNIQUE(participant_id, session_id, report_date))""",
    "CREATE INDEX ix_reports_session_status ON daily_reports(session_id, status)",
    """CREATE TABLE report_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id INTEGER NOT NULL REFERENCES daily_reports(id),
        version INTEGER NOT NULL,
        action TEXT NOT NULL,
        snapshot TEXT NOT NULL,
        actor_id INTEGER,
        note TEXT,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE day_status(
        participant_id INTEGER NOT NULL REFERENCES participants(id),
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        status_date TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('GREEN','HALF','RED','HEALTH','EMPTY')),
        set_by INTEGER NOT NULL,
        reason TEXT,
        set_at TEXT NOT NULL,
        PRIMARY KEY(participant_id, session_id, status_date))""",
    """CREATE TABLE penalties(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participant_id INTEGER NOT NULL REFERENCES participants(id),
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        penalty_date TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('wasted','partial','health')),
        amount INTEGER NOT NULL CHECK(amount >= 0),
        reason TEXT NOT NULL,
        admin_id INTEGER NOT NULL,
        is_auto INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','reversed')),
        reversed_by INTEGER,
        reversed_at TEXT,
        reversal_reason TEXT)""",
    # one ACTIVE penalty per participant per day: blocks accidental duplicates
    """CREATE UNIQUE INDEX ux_penalty_one_per_day
        ON penalties(participant_id, session_id, penalty_date) WHERE status = 'active'""",
    """CREATE TABLE audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        actor_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        entity TEXT,
        entity_id TEXT,
        details TEXT)""",
    "CREATE TABLE bot_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE pending_actions(
        token TEXT PRIMARY KEY,
        actor_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE reminders_sent(
        key TEXT PRIMARY KEY,
        participant_id INTEGER,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
]

SESSION_COLUMNS = {  # columns an admin may edit -> type
    "name": str, "group_name": str, "start_date": str, "end_date": str,
    "full_green_minutes": int, "tolerance_minutes": int, "rounding_policy": str,
    "points_per_hour": int, "max_exercise_points": int, "subject_min_questions": int,
    "subject_point_value": int, "penalty_wasted": int, "penalty_partial": int,
    "penalty_health": int, "apply_partial_penalty": int, "health_penalty_confirmed": int,
    "target_deadline": str, "report_window_days": int,
}


def _migration_1(conn):
    """Create the full schema, the first session, default settings; absorb legacy data."""
    legacy = _has_table(conn, "p") and _has_table(conn, "l")
    for stmt in SCHEMA_V1:
        conn.execute(stmt)
    d = config.DEFAULT_SESSION
    cur = conn.execute(
        """INSERT INTO sessions(name, session_number, group_name, start_date, end_date,
           full_green_minutes, tolerance_minutes, target_deadline, is_active, created_at)
           VALUES(?,?,?,?,?,?,?,?,1,?)""",
        (d["name"], d["session_number"], d["group_name"], d["start_date"], d["end_date"],
         d["full_green_minutes"], d["tolerance_minutes"], d["target_deadline"], config.now_iso()),
    )
    session_id = cur.lastrowid
    for k, v in config.DEFAULT_BOT_SETTINGS.items():
        conn.execute("INSERT INTO bot_settings(key, value) VALUES(?,?)", (k, v))
    if legacy:
        _import_legacy(conn, session_id)


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _import_legacy(conn, session_id):
    """Convert the old single-session tables p(id,name,type) and l(pid,day,hours,ex,subj,status)."""
    rules = scoring.Rules.from_row(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
    now = config.now_iso()
    n_people = n_logs = 0
    for p in conn.execute("SELECT id, name, type FROM p").fetchall():
        kind = "school" if "school" in (p["type"] or "").lower() and "dummy" not in (p["type"] or "").lower() else "dummy"
        conn.execute(
            "INSERT INTO participants(id, display_name, school_type, registered_at) VALUES(?,?,?,?)",
            (p["id"], p["name"] or f"Participant {p['id']}", kind, now))
        conn.execute("INSERT INTO session_members(session_id, participant_id, enrolled_at) VALUES(?,?,?)",
                     (session_id, p["id"], now))
        n_people += 1
    for r in conn.execute("SELECT pid, day, hours, ex, subj, status FROM l").fetchall():
        if conn.execute("SELECT 1 FROM participants WHERE id=?", (r["pid"],)).fetchone() is None:
            continue  # orphan row: keep it in legacy_l only
        n_logs += 1
        if r["status"] in ("wasted", "health"):
            st = scoring.RED if r["status"] == "wasted" else scoring.HEALTH
            conn.execute("INSERT OR IGNORE INTO day_status VALUES(?,?,?,?,?,?,?)",
                         (r["pid"], session_id, r["day"], st, 0, "Migrated from legacy bot", now))
            amount = rules.penalty_wasted if r["status"] == "wasted" else rules.penalty_health
            conn.execute(
                """INSERT OR IGNORE INTO penalties(participant_id, session_id, penalty_date, kind, amount,
                   reason, admin_id, is_auto, created_at) VALUES(?,?,?,?,?,?,0,0,?)""",
                (r["pid"], session_id, r["day"], r["status"], amount, "Migrated from legacy bot", now))
            continue
        minutes = int(round(float(r["hours"] or 0) * 60))
        subj = max(0, min(3, int(r["subj"] or 0)))
        q = [rules.subject_min_questions if i < subj else 0 for i in range(3)]
        conn.execute(
            """INSERT INTO daily_reports(participant_id, session_id, report_date, independent_minutes,
               exercise_count, physics_q, chemistry_q, biology_q, notes, status, entered_by_admin,
               submitted_at, updated_at, reviewed_by, reviewed_at)
               VALUES(?,?,?,?,?,?,?,?,?,'approved',1,?,?,0,?)""",
            (r["pid"], session_id, r["day"], minutes, max(0, int(r["ex"] or 0)), q[0], q[1], q[2],
             f"Migrated from legacy bot ({subj} subject(s) completed)", now, now, now))
        if rules.apply_partial_penalty and scoring.is_partial(minutes, rules):
            conn.execute(
                """INSERT OR IGNORE INTO penalties(participant_id, session_id, penalty_date, kind, amount,
                   reason, admin_id, is_auto, created_at) VALUES(?,?,?,'partial',?,?,0,1,?)""",
                (r["pid"], session_id, r["day"], rules.penalty_partial, "Migrated: partial day", now))
    conn.execute("ALTER TABLE p RENAME TO legacy_p")  # old data kept, never dropped
    conn.execute("ALTER TABLE l RENAME TO legacy_l")
    conn.execute("INSERT INTO audit_log(created_at, actor_id, action, entity, details) VALUES(?,?,?,?,?)",
                 (now, 0, "migrate_legacy", "database", json.dumps({"participants": n_people, "logs": n_logs})))
    log.info("Imported legacy data: %s participants, %s daily logs", n_people, n_logs)


MIGRATIONS = [(1, _migration_1)]  # append (2, fn), (3, fn)... for future schema changes


def schema_version(conn) -> int:
    if getattr(conn, "is_remote", False):
        # Turso/libSQL does not reliably support SQLite PRAGMA user_version.
        # Keep the schema version in the application settings table instead.
        try:
            row = conn.execute(
                "SELECT value FROM bot_settings WHERE key='schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            # The table does not exist yet in a brand-new database.
            return 0

    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _set_schema_version(conn, version):
    version = int(version)
    if getattr(conn, "is_remote", False):
        # Do not send PRAGMA statements to Turso; store the version in bot_settings.
        conn.execute(
            "INSERT INTO bot_settings(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )
        return

    conn.execute(f"PRAGMA user_version={version}")


def migrate(conn, backup_dir, migrations=None) -> int:
    migrations = MIGRATIONS if migrations is None else migrations
    current = schema_version(conn)
    latest = max((v for v, _ in migrations), default=0)
    if current > latest:
        raise RuntimeError(f"Database schema v{current} is newer than this bot (v{latest}). Update the bot.")
    todo = [(v, fn) for v, fn in sorted(migrations, key=lambda m: m[0]) if v > current]
    if not todo:
        return current
    has_data = current > 0 or _has_table(conn, "p")
    if has_data:  # never migrate existing records without a safety copy
        backup_database(conn, backup_dir, label=f"pre-migration-v{current}")
    for version, fn in todo:
        with transaction(conn):
            fn(conn)
            _set_schema_version(conn, version)
        log.info("Database migrated to schema v%s", version)
    return schema_version(conn)


def open_database(db_path, backup_dir):
    conn = connect(db_path)
    if getattr(conn, "is_remote", False):
        conn.wait_until_ready()  # retries ~35 s, then raises DatabaseUnavailable. Never falls back to a local file.
    migrate(conn, backup_dir)
    return conn


# --------------------------------------------------------------------- sessions
def get_session(conn, session_id):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def get_active_session(conn):
    return conn.execute("SELECT * FROM sessions WHERE is_active=1").fetchone()


def list_sessions(conn):
    return conn.execute("SELECT * FROM sessions ORDER BY session_number DESC, id DESC").fetchall()


def create_session(conn, name, session_number, group_name, start_date, end_date, copy_rules_from=None, make_active=True):
    """New session. Rules are copied from another session when given; old data is untouched."""
    base = {}
    if copy_rules_from is not None:
        src = get_session(conn, copy_rules_from)
        base = {k: src[k] for k in SESSION_COLUMNS if k not in ("name", "group_name", "start_date", "end_date")}
    cols = ["name", "session_number", "group_name", "start_date", "end_date", "created_at"] + list(base)
    vals = [name, session_number, group_name, start_date, end_date, config.now_iso()] + list(base.values())
    with transaction(conn):
        if make_active:
            conn.execute("UPDATE sessions SET is_active=0 WHERE is_active=1")
        cur = conn.execute(
            f"INSERT INTO sessions({','.join(cols)}, is_active) VALUES({','.join('?' * len(cols))}, ?)",
            vals + [1 if make_active else 0])
    return cur.lastrowid


def update_session(conn, session_id, **fields):
    for k in fields:
        if k not in SESSION_COLUMNS:
            raise ValueError(f"Unknown session setting: {k}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", [*fields.values(), session_id])


def archive_session(conn, session_id):
    conn.execute("UPDATE sessions SET archived=1, is_active=0 WHERE id=?", (session_id,))


def activate_session(conn, session_id):
    with transaction(conn):
        conn.execute("UPDATE sessions SET is_active=0 WHERE is_active=1")
        conn.execute("UPDATE sessions SET is_active=1, archived=0 WHERE id=?", (session_id,))


# --------------------------------------------------------------------- settings, audit, tokens
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO bot_settings(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def audit(conn, actor_id, action, entity=None, entity_id=None, details=None):
    conn.execute(
        "INSERT INTO audit_log(created_at, actor_id, action, entity, entity_id, details) VALUES(?,?,?,?,?,?)",
        (config.now_iso(), int(actor_id), action, entity, None if entity_id is None else str(entity_id),
         json.dumps(details, default=str, ensure_ascii=False) if details is not None else None))


def create_pending_action(conn, actor_id, action, payload, ttl_seconds=600) -> str:
    """One-time, expiring confirmation token (used for 'Are you sure?' buttons)."""
    token = secrets.token_urlsafe(6)
    now = config.now()
    conn.execute(
        "INSERT INTO pending_actions(token, actor_id, action, payload, created_at, expires_at) VALUES(?,?,?,?,?,?)",
        (token, int(actor_id), action, json.dumps(payload, default=str),
         now.isoformat(timespec="seconds"), (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")))
    return token


def consume_pending_action(conn, token, actor_id):
    """Returns (action, payload) exactly once, or None if expired/used/not yours."""
    now_s = config.now_iso()
    with transaction(conn):
        row = conn.execute(
            "SELECT action, payload FROM pending_actions WHERE token=? AND actor_id=? AND used=0 AND expires_at>=?",
            (token, int(actor_id), now_s)).fetchone()
        if row is None:
            return None
        cur = conn.execute("UPDATE pending_actions SET used=1 WHERE token=? AND used=0", (token,))
        if cur.rowcount != 1:
            return None
    return row["action"], json.loads(row["payload"])


def purge_old_pending_actions(conn):
    cutoff = (config.now() - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM pending_actions WHERE expires_at < ?", (cutoff,))
