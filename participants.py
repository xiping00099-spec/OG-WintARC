"""participants.py - registering, editing, enrolling and deactivating participants.

Nothing here ever deletes history: "removal" only flips flags.
"""
import config
import database as db
from utils import ValidationError, clean_text

SCHOOL_TYPES = ("school", "dummy")
SCHOOL_LABELS = {"school": "School-going", "dummy": "Dummy-school"}


def get(conn, participant_id):
    return conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()


def get_by_telegram(conn, telegram_user_id):
    return conn.execute("SELECT * FROM participants WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()


def is_member(conn, session_id, participant_id) -> bool:
    row = conn.execute("SELECT is_active FROM session_members WHERE session_id=? AND participant_id=?",
                       (session_id, participant_id)).fetchone()
    return bool(row and row["is_active"])


def list_members(conn, session_id, only_active=True):
    sql = ("SELECT p.*, m.is_active AS member_active FROM session_members m "
           "JOIN participants p ON p.id = m.participant_id WHERE m.session_id=?")
    if only_active:
        sql += " AND m.is_active=1 AND p.is_active=1"
    return conn.execute(sql + " ORDER BY lower(p.display_name), p.id", (session_id,)).fetchall()


def enroll(conn, session_id, participant_id):
    """Add to a session (or re-add after removal). History is kept."""
    now = config.now_iso()
    conn.execute(
        """INSERT INTO session_members(session_id, participant_id, enrolled_at) VALUES(?,?,?)
           ON CONFLICT(session_id, participant_id) DO UPDATE SET is_active=1, removed_at=NULL""",
        (session_id, participant_id, now))


def remove_from_session(conn, session_id, participant_id):
    conn.execute("UPDATE session_members SET is_active=0, removed_at=? WHERE session_id=? AND participant_id=?",
                 (config.now_iso(), session_id, participant_id))


def register(conn, display_name, school_type, session_id, telegram_user_id=None, username=None, registered_by=None):
    """Self-registration (telegram_user_id given) or admin import (usually no Telegram ID)."""
    name = clean_text(display_name, 40, "Name", allow_empty=False)
    if school_type not in SCHOOL_TYPES:
        raise ValidationError("School type must be 'school' or 'dummy'.")
    username = (username or "").lstrip("@") or None
    with db.transaction(conn):
        if telegram_user_id is not None:
            existing = get_by_telegram(conn, telegram_user_id)
            if existing:
                if not existing["is_active"]:
                    raise ValidationError("Your profile is deactivated. Please ask an admin to reactivate it.")
                enroll(conn, session_id, existing["id"])
                return existing
        cur = conn.execute(
            """INSERT INTO participants(telegram_user_id, username, display_name, school_type, registered_at, registered_by)
               VALUES(?,?,?,?,?,?)""",
            (telegram_user_id, username, name, school_type, config.now_iso(), registered_by))
        enroll(conn, session_id, cur.lastrowid)
        if registered_by is not None:
            db.audit(conn, registered_by, "participant_add", "participant", cur.lastrowid,
                     {"name": name, "type": school_type, "telegram_user_id": telegram_user_id})
    return get(conn, cur.lastrowid)


def update(conn, participant_id, actor_id, **fields):
    allowed = {"display_name", "school_type", "username"}
    if not fields or set(fields) - allowed:
        raise ValueError("Unsupported field")
    if "display_name" in fields:
        fields["display_name"] = clean_text(fields["display_name"], 40, "Name", allow_empty=False)
    if "school_type" in fields and fields["school_type"] not in SCHOOL_TYPES:
        raise ValidationError("School type must be 'school' or 'dummy'.")
    old = get(conn, participant_id)
    if old is None:
        raise ValidationError("Participant not found.")
    with db.transaction(conn):
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE participants SET {sets} WHERE id=?", [*fields.values(), participant_id])
        db.audit(conn, actor_id, "participant_edit", "participant", participant_id,
                 {k: [old[k], v] for k, v in fields.items()})


def set_active(conn, participant_id, active: bool, actor_id):
    with db.transaction(conn):
        conn.execute("UPDATE participants SET is_active=? WHERE id=?", (1 if active else 0, participant_id))
        db.audit(conn, actor_id, "participant_reactivate" if active else "participant_deactivate",
                 "participant", participant_id)


def link_telegram(conn, participant_id, telegram_user_id, actor_id):
    if get_by_telegram(conn, telegram_user_id):
        raise ValidationError("That Telegram ID is already linked to another participant.")
    with db.transaction(conn):
        conn.execute("UPDATE participants SET telegram_user_id=? WHERE id=?", (telegram_user_id, participant_id))
        db.audit(conn, actor_id, "participant_link", "participant", participant_id, {"telegram_user_id": telegram_user_id})


def refresh_username(conn, tg_user):
    """Usernames change; the Telegram user ID is the stable identity. Keep username current."""
    row = get_by_telegram(conn, tg_user.id)
    if row and (row["username"] or None) != (tg_user.username or None):
        conn.execute("UPDATE participants SET username=? WHERE id=?", (tg_user.username, row["id"]))
    return row


def label(row) -> str:
    """Human label that works even without a username."""
    return row["display_name"]
