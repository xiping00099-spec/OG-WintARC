"""
reports.py - the service layer: daily targets, daily reports, penalties, day-status overrides
and the per-participant statistics that every other module (leaderboard, chart, export,
dashboard) reuses. Points are NEVER stored: they are recomputed from approved reports,
so editing a report can never "add points on top of old points".
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta

import config
import database as db
import participants as ppl
import scoring
from scoring import Rules
from utils import ValidationError, clean_text

log = logging.getLogger(__name__)

REPORT_STATES = ("draft", "pending", "approved", "rejected", "correction_requested")
PENALTY_KINDS = ("wasted", "partial", "health")


# ------------------------------------------------------------------ small helpers
def parse_date(text) -> date:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Dates must look like 2026-11-15 (YYYY-MM-DD).")


def rules_of(session) -> Rules:
    return Rules.from_row(session)


def session_range(session):
    return parse_date(session["start_date"]), parse_date(session["end_date"])


def total_minutes(report) -> int:
    return int(report["independent_minutes"]) + int(report["school_minutes"])


def _check_in_session(session, d: date, label="Date"):
    start, end = session_range(session)
    if not start <= d <= end:
        raise ValidationError(f"{label} {d.isoformat()} is outside the session ({start} to {end}).")


def _check_identity(participant, actor_id, by_admin):
    """Server-side rule: participants may only act for themselves."""
    if by_admin:
        return
    if participant["telegram_user_id"] is None or participant["telegram_user_id"] != actor_id:
        raise ValidationError("You can only submit your own targets and reports.")
    if not participant["is_active"]:
        raise ValidationError("Your profile is deactivated. Please contact an admin.")


# ------------------------------------------------------------------ validation
def validate_report_fields(data: dict, participant) -> dict:
    def num(key, maximum, label):
        v = data.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValidationError(f"{label} must be a whole number.")
        if v < 0:
            raise ValidationError(f"{label} cannot be negative.")
        if v > maximum:
            raise ValidationError(f"{label} cannot be more than {maximum}.")
        return v

    clean = {
        "independent_minutes": num("independent_minutes", scoring.MAX_MINUTES_PER_FIELD, "Independent study time"),
        "school_minutes": num("school_minutes", scoring.MAX_MINUTES_PER_FIELD, "School study time"),
        "exercise_count": num("exercise_count", scoring.MAX_EXERCISE_COUNT, "Exercise count"),
        "physics_q": num("physics_q", scoring.MAX_QUESTIONS, "Physics questions"),
        "chemistry_q": num("chemistry_q", scoring.MAX_QUESTIONS, "Chemistry questions"),
        "biology_q": num("biology_q", scoring.MAX_QUESTIONS, "Biology questions"),
        "exercise_names": clean_text(data.get("exercise_names", ""), 200, "Exercise names"),
        "notes": clean_text(data.get("notes", ""), 500, "Notes"),
    }
    if participant["school_type"] == "dummy" and clean["school_minutes"] > 0:
        raise ValidationError("Dummy-school participants have no school study time (enter 0).")
    if clean["independent_minutes"] + clean["school_minutes"] > 24 * 60:
        raise ValidationError("Total study time cannot exceed 24 hours in one day.")
    if clean["exercise_count"] == 0:
        clean["exercise_names"] = ""
    return clean


def validate_target_fields(data: dict) -> dict:
    def num(key, maximum, label):
        v = data.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > maximum:
            raise ValidationError(f"{label} must be a whole number between 0 and {maximum}.")
        return v

    return {
        "planned_minutes": num("planned_minutes", 24 * 60, "Planned study time"),
        "planned_physics": num("planned_physics", scoring.MAX_QUESTIONS, "Planned Physics questions"),
        "planned_chemistry": num("planned_chemistry", scoring.MAX_QUESTIONS, "Planned Chemistry questions"),
        "planned_biology": num("planned_biology", scoring.MAX_QUESTIONS, "Planned Biology questions"),
        "planned_exercises": num("planned_exercises", scoring.MAX_EXERCISE_COUNT, "Planned exercises"),
        "notes": clean_text(data.get("notes", ""), 500, "Notes"),
    }


# ------------------------------------------------------------------ daily targets
def target_deadline(session, target_date: date) -> datetime:
    """Targets for day D must be in by `target_deadline` (default 09:00 IST) on day D."""
    hh, mm = (int(x) for x in session["target_deadline"].split(":"))
    return datetime.combine(target_date, dtime(hh, mm), tzinfo=config.TZ)


def get_target(conn, participant_id, session_id, target_date: date):
    return conn.execute(
        "SELECT * FROM daily_targets WHERE participant_id=? AND session_id=? AND target_date=?",
        (participant_id, session_id, target_date.isoformat())).fetchone()


def submit_target(conn, participant, session, target_date: date, data: dict, actor_id, by_admin=False, now=None):
    """Create or edit a target. Every version is kept in target_history."""
    now = now or config.now()
    _check_identity(participant, actor_id, by_admin)
    _check_in_session(session, target_date, "Target date")
    if not ppl.is_member(conn, session["id"], participant["id"]):
        raise ValidationError("You are not enrolled in this session.")
    if target_date < now.date() and not by_admin:
        raise ValidationError("That day is already over - targets can no longer be set for it.")
    late = now > target_deadline(session, target_date)
    if late and not by_admin:
        raise ValidationError(
            f"The deadline for {target_date.isoformat()} targets was {session['target_deadline']} IST, so it is closed.")
    clean = validate_target_fields(data)
    ts = now.isoformat(timespec="seconds")
    with db.transaction(conn):
        existing = get_target(conn, participant["id"], session["id"], target_date)
        if existing:
            version = existing["version"] + 1
            conn.execute(
                """UPDATE daily_targets SET planned_minutes=?, planned_physics=?, planned_chemistry=?,
                   planned_biology=?, planned_exercises=?, notes=?, version=?, updated_at=? WHERE id=?""",
                (clean["planned_minutes"], clean["planned_physics"], clean["planned_chemistry"],
                 clean["planned_biology"], clean["planned_exercises"], clean["notes"], version, ts, existing["id"]))
            tid = existing["id"]
        else:
            version = 1
            cur = conn.execute(
                """INSERT INTO daily_targets(participant_id, session_id, target_date, planned_minutes,
                   planned_physics, planned_chemistry, planned_biology, planned_exercises, notes, version,
                   is_late, submitted_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (participant["id"], session["id"], target_date.isoformat(), clean["planned_minutes"],
                 clean["planned_physics"], clean["planned_chemistry"], clean["planned_biology"],
                 clean["planned_exercises"], clean["notes"], version, 1 if late else 0, ts, ts))
            tid = cur.lastrowid
        conn.execute("INSERT INTO target_history(target_id, version, snapshot, actor_id, created_at) VALUES(?,?,?,?,?)",
                     (tid, version, json.dumps(clean, ensure_ascii=False), actor_id, ts))
        if by_admin:
            db.audit(conn, actor_id, "admin_target", "target", tid, clean)
    return conn.execute("SELECT * FROM daily_targets WHERE id=?", (tid,)).fetchone(), existing is None


# ------------------------------------------------------------------ daily reports
def get_report(conn, participant_id, session_id, report_date: date):
    return conn.execute(
        "SELECT * FROM daily_reports WHERE participant_id=? AND session_id=? AND report_date=?",
        (participant_id, session_id, report_date.isoformat())).fetchone()


def get_report_by_id(conn, report_id):
    return conn.execute("SELECT * FROM daily_reports WHERE id=?", (report_id,)).fetchone()


def _snapshot(row) -> str:
    keys = ("independent_minutes", "school_minutes", "exercise_count", "exercise_names", "physics_q",
            "chemistry_q", "biology_q", "notes", "status")
    return json.dumps({k: row[k] for k in keys}, ensure_ascii=False)


def _history(conn, report_id, action, actor_id, note=None):
    row = get_report_by_id(conn, report_id)
    conn.execute(
        "INSERT INTO report_history(report_id, version, action, snapshot, actor_id, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (report_id, row["version"], action, _snapshot(row), actor_id, note, config.now_iso()))


def provisional_points(report, session):
    """Points this report WOULD earn if approved (shown to the participant as provisional)."""
    return scoring.compute_daily_points(total_minutes(report), report["exercise_count"], report["physics_q"],
                                        report["chemistry_q"], report["biology_q"], rules_of(session))


def submit_report(conn, participant, session, report_date: date, data: dict, actor_id, by_admin=False, today=None):
    """
    Participant submission (status -> pending) or admin entry (status -> approved).
    One report per participant/session/date; re-submitting replaces the content and
    keeps the old version in report_history.
    """
    today = today or config.today()
    _check_identity(participant, actor_id, by_admin)
    _check_in_session(session, report_date, "Report date")
    if report_date > today:
        raise ValidationError("You can't report a future date.")
    if not ppl.is_member(conn, session["id"], participant["id"]) and not by_admin:
        raise ValidationError("You are not enrolled in this session.")
    if not by_admin and report_date < today - timedelta(days=session["report_window_days"] - 1):
        raise ValidationError(f"Reports can only be submitted for the last {session['report_window_days']} day(s). "
                              "Ask an admin to enter older days.")
    clean = validate_report_fields(data, participant)
    ts = config.now_iso()
    new_status = "approved" if by_admin else "pending"
    with db.transaction(conn):
        existing = get_report(conn, participant["id"], session["id"], report_date)
        if existing:
            if existing["status"] == "approved" and not by_admin:
                raise ValidationError("This report is already approved. Ask an admin if it needs a correction.")
            conn.execute(
                """UPDATE daily_reports SET independent_minutes=?, school_minutes=?, exercise_count=?, exercise_names=?,
                   physics_q=?, chemistry_q=?, biology_q=?, notes=?, status=?, version=version+1, entered_by_admin=?,
                   updated_at=?, reviewed_by=?, reviewed_at=?, review_note=NULL WHERE id=?""",
                (clean["independent_minutes"], clean["school_minutes"], clean["exercise_count"], clean["exercise_names"],
                 clean["physics_q"], clean["chemistry_q"], clean["biology_q"], clean["notes"], new_status,
                 1 if by_admin else 0, ts, actor_id if by_admin else None, ts if by_admin else None, existing["id"]))
            rid, action = existing["id"], ("admin_entry" if by_admin else "resubmitted")
        else:
            cur = conn.execute(
                """INSERT INTO daily_reports(participant_id, session_id, report_date, independent_minutes, school_minutes,
                   exercise_count, exercise_names, physics_q, chemistry_q, biology_q, notes, status, entered_by_admin,
                   submitted_at, updated_at, reviewed_by, reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (participant["id"], session["id"], report_date.isoformat(), clean["independent_minutes"],
                 clean["school_minutes"], clean["exercise_count"], clean["exercise_names"], clean["physics_q"],
                 clean["chemistry_q"], clean["biology_q"], clean["notes"], new_status, 1 if by_admin else 0, ts, ts,
                 actor_id if by_admin else None, ts if by_admin else None))
            rid, action = cur.lastrowid, ("admin_entry" if by_admin else "submitted")
        _history(conn, rid, action, actor_id)
        if by_admin:
            db.audit(conn, actor_id, "admin_report", "report", rid, clean)
        sync_partial_penalty(conn, participant["id"], session, report_date)
    return get_report_by_id(conn, rid)


def review_report(conn, report_id, admin_id, decision, note=None):
    """decision: approve | reject | correction. Only a *pending* report can be reviewed (safe to press twice)."""
    target = {"approve": "approved", "reject": "rejected", "correction": "correction_requested"}.get(decision)
    if target is None:
        raise ValueError("bad decision")
    note = clean_text(note, 300, "Note")
    if target != "approved" and not note:
        raise ValidationError("A reason is required when rejecting or requesting a correction.")
    with db.transaction(conn):
        rep = get_report_by_id(conn, report_id)
        if rep is None:
            raise ValidationError("Report not found.")
        if rep["status"] != "pending":
            raise ValidationError(f"This report is already '{rep['status'].replace('_', ' ')}'.")
        ts = config.now_iso()
        conn.execute("UPDATE daily_reports SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, "
                     "version=version+1, updated_at=? WHERE id=?", (target, admin_id, ts, note or None, ts, report_id))
        _history(conn, report_id, target, admin_id, note)
        db.audit(conn, admin_id, f"report_{target}", "report", report_id, {"note": note})
        session = db.get_session(conn, rep["session_id"])
        sync_partial_penalty(conn, rep["participant_id"], session, parse_date(rep["report_date"]))
    return get_report_by_id(conn, report_id)


def admin_edit_report(conn, report_id, admin_id, changes: dict, reason):
    """Admin correction. Points are recalculated automatically (they are derived, not stored)."""
    reason = clean_text(reason, 300, "Reason", allow_empty=False)
    with db.transaction(conn):
        rep = get_report_by_id(conn, report_id)
        if rep is None:
            raise ValidationError("Report not found.")
        participant = ppl.get(conn, rep["participant_id"])
        merged = {k: rep[k] for k in ("independent_minutes", "school_minutes", "exercise_count", "exercise_names",
                                      "physics_q", "chemistry_q", "biology_q", "notes")}
        merged.update(changes)
        clean = validate_report_fields(merged, participant)
        ts = config.now_iso()
        conn.execute(
            """UPDATE daily_reports SET independent_minutes=?, school_minutes=?, exercise_count=?, exercise_names=?,
               physics_q=?, chemistry_q=?, biology_q=?, notes=?, version=version+1, updated_at=? WHERE id=?""",
            (clean["independent_minutes"], clean["school_minutes"], clean["exercise_count"], clean["exercise_names"],
             clean["physics_q"], clean["chemistry_q"], clean["biology_q"], clean["notes"], ts, report_id))
        _history(conn, report_id, "admin_edit", admin_id, reason)
        db.audit(conn, admin_id, "report_edit", "report", report_id,
                 {"reason": reason, "before": {k: rep[k] for k in changes if k in rep.keys()}, "after": changes})
        session = db.get_session(conn, rep["session_id"])
        sync_partial_penalty(conn, rep["participant_id"], session, parse_date(rep["report_date"]))
    return get_report_by_id(conn, report_id)


def pending_reports(conn, session_id):
    return conn.execute(
        """SELECT r.*, p.display_name FROM daily_reports r JOIN participants p ON p.id=r.participant_id
           WHERE r.session_id=? AND r.status='pending' ORDER BY r.report_date, r.submitted_at, r.id""",
        (session_id,)).fetchall()


def recent_reports(conn, session_id, limit=200):
    return conn.execute(
        """SELECT r.*, p.display_name FROM daily_reports r JOIN participants p ON p.id=r.participant_id
           WHERE r.session_id=? ORDER BY r.report_date DESC, r.id DESC LIMIT ?""", (session_id, limit)).fetchall()


# ------------------------------------------------------------------ day status overrides
def set_day_status(conn, participant_id, session, status_date: date, status, admin_id, reason):
    if status not in scoring.OVERRIDE_STATUSES:
        raise ValidationError("Unknown status.")
    reason = clean_text(reason, 300, "Reason", allow_empty=False)
    _check_in_session(session, status_date)
    with db.transaction(conn):
        old = conn.execute("SELECT status FROM day_status WHERE participant_id=? AND session_id=? AND status_date=?",
                           (participant_id, session["id"], status_date.isoformat())).fetchone()
        conn.execute(
            """INSERT INTO day_status(participant_id, session_id, status_date, status, set_by, reason, set_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(participant_id, session_id, status_date)
               DO UPDATE SET status=excluded.status, set_by=excluded.set_by, reason=excluded.reason, set_at=excluded.set_at""",
            (participant_id, session["id"], status_date.isoformat(), status, admin_id, reason, config.now_iso()))
        db.audit(conn, admin_id, "day_status_set", "participant", participant_id,
                 {"date": status_date.isoformat(), "from": old["status"] if old else None, "to": status, "reason": reason})


def clear_day_status(conn, participant_id, session, status_date: date, admin_id, reason):
    reason = clean_text(reason, 300, "Reason", allow_empty=False)
    with db.transaction(conn):
        conn.execute("DELETE FROM day_status WHERE participant_id=? AND session_id=? AND status_date=?",
                     (participant_id, session["id"], status_date.isoformat()))
        db.audit(conn, admin_id, "day_status_clear", "participant", participant_id,
                 {"date": status_date.isoformat(), "reason": reason})


# ------------------------------------------------------------------ penalties
def active_penalty(conn, participant_id, session_id, d: date):
    return conn.execute(
        "SELECT * FROM penalties WHERE participant_id=? AND session_id=? AND penalty_date=? AND status='active'",
        (participant_id, session_id, d.isoformat())).fetchone()


def penalty_amount(session, kind) -> int:
    return {"wasted": session["penalty_wasted"], "partial": session["penalty_partial"],
            "health": session["penalty_health"]}[kind]


def apply_penalty(conn, participant_id, session, penalty_date: date, kind, admin_id, reason=None, today=None):
    """
    Admin-confirmed penalty. Rules:
      * only one ACTIVE penalty per participant per day (duplicate-proof, enforced by a unique index)
      * 'health' is refused unless the session's health_penalty_confirmed policy is switched on
      * 'wasted' marks the day RED, 'health' marks the day HEALTH (status stays separate from points)
    """
    today = today or config.today()
    if kind not in PENALTY_KINDS:
        raise ValidationError("Unknown penalty type.")
    _check_in_session(session, penalty_date, "Penalty date")
    if penalty_date > today:
        raise ValidationError("You can't penalise a future date.")
    if kind == "health" and not session["health_penalty_confirmed"]:
        raise ValidationError("The health-day penalty is not enabled: the group has not confirmed it as final policy. "
                              "Record the day as HEALTH (no deduction) instead, or enable the policy in settings.")
    reason = clean_text(reason or {"wasted": "Completely wasted day", "partial": "Partial day below threshold",
                                   "health": "Health-related disruption"}[kind], 300, "Reason", allow_empty=False)
    amount = penalty_amount(session, kind)
    with db.transaction(conn):
        if active_penalty(conn, participant_id, session["id"], penalty_date):
            raise ValidationError("An active penalty already exists for this participant on that date. "
                                  "Reverse it first if it was a mistake.")
        cur = conn.execute(
            """INSERT INTO penalties(participant_id, session_id, penalty_date, kind, amount, reason, admin_id, is_auto, created_at)
               VALUES(?,?,?,?,?,?,?,0,?)""",
            (participant_id, session["id"], penalty_date.isoformat(), kind, amount, reason, admin_id, config.now_iso()))
        db.audit(conn, admin_id, "penalty_apply", "penalty", cur.lastrowid,
                 {"participant": participant_id, "date": penalty_date.isoformat(), "kind": kind, "amount": amount})
        if kind in ("wasted", "health"):
            has_override = conn.execute(
                "SELECT 1 FROM day_status WHERE participant_id=? AND session_id=? AND status_date=?",
                (participant_id, session["id"], penalty_date.isoformat())).fetchone()
            if not has_override:
                set_day_status(conn, participant_id, session, penalty_date,
                               scoring.RED if kind == "wasted" else scoring.HEALTH, admin_id, f"{kind} day: {reason}")
    return cur.lastrowid


def reverse_penalty(conn, penalty_id, admin_id, reason):
    reason = clean_text(reason, 300, "Reversal reason", allow_empty=False)
    with db.transaction(conn):
        p = conn.execute("SELECT * FROM penalties WHERE id=?", (penalty_id,)).fetchone()
        if p is None:
            raise ValidationError("Penalty not found.")
        if p["status"] != "active":
            raise ValidationError("That penalty was already reversed.")
        conn.execute("UPDATE penalties SET status='reversed', reversed_by=?, reversed_at=?, reversal_reason=? WHERE id=?",
                     (admin_id, config.now_iso(), reason, penalty_id))
        db.audit(conn, admin_id, "penalty_reverse", "penalty", penalty_id, {"reason": reason})
    return p


def sync_partial_penalty(conn, participant_id, session, d: date):
    """
    Keep the automatic partial-day penalty (-2) consistent with the report on date `d`.
    Called whenever an approved report changes. It never doubles up:
      * eligible + no penalty yet            -> one is created
      * eligible + already penalised (auto)  -> nothing happens (edits don't re-deduct)
      * no longer eligible                   -> the automatic penalty is reversed (by system)
      * an admin manually reversed it before -> it is NOT re-applied
    """
    rules = rules_of(session)
    rep = get_report(conn, participant_id, session["id"], d)
    eligible = bool(rules.apply_partial_penalty and rep and rep["status"] == "approved"
                    and scoring.is_partial(total_minutes(rep), rules))
    current = active_penalty(conn, participant_id, session["id"], d)
    if current and current["kind"] == "partial" and current["is_auto"] and not eligible:
        conn.execute("UPDATE penalties SET status='reversed', reversed_by=0, reversed_at=?, "
                     "reversal_reason='Automatic: report is no longer a partial day' WHERE id=?",
                     (config.now_iso(), current["id"]))
        db.audit(conn, 0, "penalty_auto_reverse", "penalty", current["id"])
    elif eligible and current is None:
        manual = conn.execute(
            """SELECT 1 FROM penalties WHERE participant_id=? AND session_id=? AND penalty_date=? AND kind='partial'
               AND status='reversed' AND reversed_by != 0""", (participant_id, session["id"], d.isoformat())).fetchone()
        if not manual:
            cur = conn.execute(
                """INSERT INTO penalties(participant_id, session_id, penalty_date, kind, amount, reason, admin_id, is_auto, created_at)
                   VALUES(?,?,?,'partial',?,?,0,1,?)""",
                (participant_id, session["id"], d.isoformat(), session["penalty_partial"],
                 "Partial day below the full-green threshold", config.now_iso()))
            db.audit(conn, 0, "penalty_auto_apply", "penalty", cur.lastrowid, {"date": d.isoformat()})


def list_penalties(conn, session_id, participant_id=None, include_reversed=True):
    sql = ("SELECT pe.*, p.display_name FROM penalties pe JOIN participants p ON p.id=pe.participant_id "
           "WHERE pe.session_id=?")
    args = [session_id]
    if participant_id is not None:
        sql += " AND pe.participant_id=?"
        args.append(participant_id)
    if not include_reversed:
        sql += " AND pe.status='active'"
    return conn.execute(sql + " ORDER BY pe.penalty_date DESC, pe.id DESC", args).fetchall()


# ------------------------------------------------------------------ statistics
@dataclass
class DayRecord:
    day: date
    status: str
    source: str                     # 'override' | 'report' | 'none'
    report_status: str = None
    minutes: int = 0                # VERIFIED minutes only (approved reports)
    points: scoring.DailyPoints = None
    penalty: int = 0
    future: bool = False
    report_id: int = None


@dataclass
class Summary:
    participant_id: int
    name: str
    school_type: str
    active: bool
    days: dict = field(default_factory=dict)
    verified_minutes: int = 0
    gross_points: int = 0
    penalty_total: int = 0
    current_streak: int = 0
    longest_streak: int = 0
    success_days: int = 0
    partial_days: int = 0
    unsuccessful_days: int = 0
    health_days: int = 0
    pending_reports: int = 0
    correction_requests: int = 0
    missing_reports: int = 0
    approved_reports: int = 0
    avg_minutes: float = 0.0
    physics_total: int = 0
    chemistry_total: int = 0
    biology_total: int = 0
    physics_days: int = 0
    chemistry_days: int = 0
    biology_days: int = 0
    exercise_total: int = 0
    exercise_days: int = 0
    exercise_points_total: int = 0
    study_points_total: int = 0
    subject_points_total: int = 0

    @property
    def net_points(self) -> int:
        return self.gross_points - self.penalty_total


def build_summaries(conn, session, today=None, only_active=True):
    """Compute every statistic from the database for one session (single source of truth)."""
    today = today or config.today()
    rules = rules_of(session)
    start, end = session_range(session)
    sid = session["id"]
    members = ppl.list_members(conn, sid, only_active=only_active)

    reports, overrides, penalties = {}, {}, {}
    for r in conn.execute("SELECT * FROM daily_reports WHERE session_id=?", (sid,)):
        reports[(r["participant_id"], r["report_date"])] = r
    for o in conn.execute("SELECT * FROM day_status WHERE session_id=?", (sid,)):
        overrides[(o["participant_id"], o["status_date"])] = o["status"]
    for p in conn.execute("SELECT participant_id, penalty_date, SUM(amount) AS amt FROM penalties "
                          "WHERE session_id=? AND status='active' GROUP BY participant_id, penalty_date", (sid,)):
        penalties[(p["participant_id"], p["penalty_date"])] = p["amt"]
    pen_total = {}
    for (pid, _), amt in penalties.items():
        pen_total[pid] = pen_total.get(pid, 0) + amt

    out = []
    last = min(end, today)
    for m in members:
        pid = m["id"]
        s = Summary(pid, m["display_name"], m["school_type"], bool(m["is_active"] and m["member_active"]))
        statuses = {}
        countable = 0
        for d in scoring.daterange(start, end):
            key = (pid, d.isoformat())
            rep = reports.get(key)
            override = overrides.get(key)
            future = d > today
            approved = rep is not None and rep["status"] == "approved"
            minutes = total_minutes(rep) if approved else 0
            st = scoring.day_status(rep["status"] if rep else None, minutes, rules, override)
            if future:
                st = override or scoring.EMPTY
            source = "override" if override else ("report" if rep else "none")
            pts = None
            if approved and not future:
                pts = scoring.compute_daily_points(minutes, rep["exercise_count"], rep["physics_q"],
                                                   rep["chemistry_q"], rep["biology_q"], rules)
            rec = DayRecord(d, st, source, rep["status"] if rep else None, minutes, pts,
                            penalties.get(key, 0), future, rep["id"] if rep else None)
            s.days[d] = rec
            statuses[d] = st
            if future:
                continue
            if approved:
                s.approved_reports += 1
                s.verified_minutes += minutes
                s.gross_points += pts.gross
                s.study_points_total += pts.study_points
                s.exercise_points_total += pts.exercise_points
                s.subject_points_total += pts.subject_points
                s.physics_total += rep["physics_q"]
                s.chemistry_total += rep["chemistry_q"]
                s.biology_total += rep["biology_q"]
                s.physics_days += int(pts.subjects_met[0])
                s.chemistry_days += int(pts.subjects_met[1])
                s.biology_days += int(pts.subjects_met[2])
                s.exercise_total += rep["exercise_count"]
                s.exercise_days += int(rep["exercise_count"] > 0)
            if rep is not None and rep["status"] == "pending":
                s.pending_reports += 1
            if rep is not None and rep["status"] == "correction_requested":
                s.correction_requests += 1
            if st == scoring.GREEN:
                s.success_days += 1
            elif st == scoring.HALF:
                s.partial_days += 1
            elif st == scoring.RED:
                s.unsuccessful_days += 1
            elif st == scoring.HEALTH:
                s.health_days += 1
            elif st == scoring.EMPTY and d < today:
                s.missing_reports += 1
            if d < today or approved:
                countable += 1
        s.penalty_total = pen_total.get(pid, 0)
        s.avg_minutes = (s.verified_minutes / countable) if countable else 0.0
        s.current_streak, s.longest_streak = scoring.compute_streaks(statuses, start, end, today)
        out.append(s)
    return out


def get_summary(conn, session, participant_id, today=None):
    for s in build_summaries(conn, session, today, only_active=False):
        if s.participant_id == participant_id:
            return s
    return None


# ------------------------------------------------------------------ labels
def session_title(session) -> str:
    return f"{session['name']} · Session {int(session['session_number']):02d}"


def date_range_label(session) -> str:
    s, e = session_range(session)
    return f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}".upper()


def resync_partial_penalties(conn, session_id):
    """After a rule change (threshold, tolerance, partial-penalty switch) bring every automatic
    partial-day penalty back in line with the new rules. Safe to run repeatedly."""
    session = db.get_session(conn, session_id)
    rows = conn.execute("SELECT DISTINCT participant_id, report_date FROM daily_reports WHERE session_id=?", (session_id,)).fetchall()
    rows += conn.execute("SELECT participant_id, penalty_date AS report_date FROM penalties WHERE session_id=? "
                         "AND status='active' AND kind='partial' AND is_auto=1", (session_id,)).fetchall()
    with db.transaction(conn):
        for r in {(x["participant_id"], x["report_date"]) for x in rows}:
            sync_partial_penalty(conn, r[0], session, parse_date(r[1]))
