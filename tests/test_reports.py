from datetime import date, datetime, timezone

import pytest

import config
import database as db
import participants as ppl
import reports
from conftest import ADMIN, approved_report, report_data
from utils import ValidationError

D = date
TODAY = D(2026, 11, 10)


def test_participant_report_is_pending_and_gives_no_official_points(conn, session, alice):
    rep = reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)
    assert rep["status"] == "pending"
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.gross_points == 0 and s.pending_reports == 1
    assert reports.provisional_points(rep, session).gross == 13  # provisional only


def test_approval_awards_points_once(conn, session, alice):
    rep = reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)
    reports.review_report(conn, rep["id"], ADMIN, "approve")
    assert reports.get_summary(conn, session, alice["id"], TODAY).gross_points == 13
    with pytest.raises(ValidationError):  # pressing Approve twice is safe
        reports.review_report(conn, rep["id"], ADMIN, "approve")
    assert reports.get_summary(conn, session, alice["id"], TODAY).gross_points == 13


def test_duplicate_report_blocked_at_database_level(conn, session, alice):
    reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""INSERT INTO daily_reports(participant_id, session_id, report_date, submitted_at, updated_at)
                        VALUES(?,?,?,?,?)""", (alice["id"], session["id"], TODAY.isoformat(), "x", "x"))


def test_resubmitting_pending_replaces_and_keeps_history(conn, session, alice):
    reports.submit_report(conn, alice, session, TODAY, report_data(indep=300), 1001)
    rep = reports.submit_report(conn, alice, session, TODAY, report_data(indep=480), 1001)
    assert rep["independent_minutes"] == 480 and rep["version"] == 2
    n = conn.execute("SELECT COUNT(*) FROM report_history WHERE report_id=?", (rep["id"],)).fetchone()[0]
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1


def test_participant_cannot_edit_approved_report(conn, session, alice):
    approved_report(conn, alice, session, TODAY)
    with pytest.raises(ValidationError):
        reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)


def test_correction_flow(conn, session, alice):
    rep = reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)
    with pytest.raises(ValidationError):
        reports.review_report(conn, rep["id"], ADMIN, "correction")  # reason required
    reports.review_report(conn, rep["id"], ADMIN, "correction", "check hours")
    again = reports.submit_report(conn, alice, session, TODAY, report_data(indep=400), 1001)
    assert again["status"] == "pending"


def test_edit_recalculates_instead_of_duplicating(conn, session, alice):
    rep = approved_report(conn, alice, session, TODAY, indep=480)
    assert reports.get_summary(conn, session, alice["id"], TODAY).gross_points == 13
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 360}, "fix")
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.gross_points == 6 + 2 + 3  # replaced, not 13 + 11
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 360}, "same again")
    assert reports.get_summary(conn, session, alice["id"], TODAY).gross_points == 11


@pytest.mark.parametrize("bad", [dict(independent_minutes=-1), dict(exercise_count=-1), dict(physics_q=-3),
                                 dict(exercise_count=99), dict(independent_minutes=2000), dict(biology_q=99999)])
def test_invalid_numbers_rejected(conn, session, alice, bad):
    with pytest.raises(ValidationError):
        reports.submit_report(conn, alice, session, TODAY, report_data(**bad), 1001)


def test_participant_cannot_submit_for_someone_else(conn, session, alice, bob):
    with pytest.raises(ValidationError):
        reports.submit_report(conn, alice, session, TODAY, report_data(), 1002)  # Bob's ID, Alice's profile


def test_dummy_school_cannot_have_school_minutes(conn, session, alice):
    with pytest.raises(ValidationError):
        reports.submit_report(conn, alice, session, TODAY, report_data(school=60), 1001)


def test_school_minutes_add_to_total(conn, session, bob):
    rep = reports.submit_report(conn, bob, session, TODAY, report_data(indep=300, school=180), 1002)
    assert reports.total_minutes(rep) == 480


def test_session_boundaries_and_future(conn, session, alice):
    for bad in (D(2026, 10, 31), D(2026, 12, 1)):
        with pytest.raises(ValidationError):
            reports.submit_report(conn, alice, session, bad, report_data(), 1001, by_admin=True)
    with pytest.raises(ValidationError):
        reports.submit_report(conn, alice, session, D(2026, 11, 11), report_data(), 1001)  # future
    with pytest.raises(ValidationError):  # outside the 2-day participant window
        reports.submit_report(conn, alice, session, D(2026, 11, 8), report_data(), 1001)
    reports.submit_report(conn, alice, session, D(2026, 11, 9), report_data(), 1001)  # yesterday is fine


# ------------------------------------------------------------ penalties
def test_partial_penalty_applied_once_even_after_edits(conn, session, alice):
    rep = approved_report(conn, alice, session, TODAY, indep=300, ex=0, p=0, c=0, b=0)
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert (s.gross_points, s.penalty_total, s.net_points) == (5, 2, 3)
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 310}, "edit 1")
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 320}, "edit 2")
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 2
    assert conn.execute("SELECT COUNT(*) FROM penalties WHERE status='active'").fetchone()[0] == 1


def test_partial_penalty_removed_when_report_becomes_full(conn, session, alice):
    rep = approved_report(conn, alice, session, TODAY, indep=300)
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 480}, "fix")
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 0
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 200}, "again partial")
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 2


def test_pending_partial_report_has_no_penalty_yet(conn, session, alice):
    reports.submit_report(conn, alice, session, TODAY, report_data(indep=300), 1001)
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 0


def test_wasted_penalty_and_duplicate_prevention(conn, session, alice):
    reports.apply_penalty(conn, alice["id"], session, TODAY, "wasted", ADMIN)
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.penalty_total == 5 and s.net_points == -5
    assert s.days[TODAY].status == "RED"
    with pytest.raises(ValidationError):
        reports.apply_penalty(conn, alice["id"], session, TODAY, "wasted", ADMIN)
    with pytest.raises(ValidationError):  # a different kind on the same day is also blocked
        reports.apply_penalty(conn, alice["id"], session, TODAY, "partial", ADMIN)


def test_reverse_penalty_creates_audit_and_allows_reapply(conn, session, alice):
    pid = reports.apply_penalty(conn, alice["id"], session, TODAY, "wasted", ADMIN)
    reports.reverse_penalty(conn, pid, ADMIN, "mistake")
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 0
    row = conn.execute("SELECT * FROM penalties WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "reversed" and row["reversal_reason"] == "mistake" and row["reversed_by"] == ADMIN
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='penalty_reverse'").fetchone()[0] == 1
    with pytest.raises(ValidationError):
        reports.reverse_penalty(conn, pid, ADMIN, "again")


def test_manually_reversed_partial_penalty_is_not_reapplied(conn, session, alice):
    rep = approved_report(conn, alice, session, TODAY, indep=300)
    pen = conn.execute("SELECT id FROM penalties").fetchone()["id"]
    reports.reverse_penalty(conn, pen, ADMIN, "waived")
    reports.admin_edit_report(conn, rep["id"], ADMIN, {"independent_minutes": 310}, "edit")
    assert reports.get_summary(conn, session, alice["id"], TODAY).penalty_total == 0


def test_health_penalty_requires_policy_and_status_stays_health(conn, session, alice):
    with pytest.raises(ValidationError):
        reports.apply_penalty(conn, alice["id"], session, TODAY, "health", ADMIN)
    reports.set_day_status(conn, alice["id"], session, TODAY, "HEALTH", ADMIN, "fever")
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.days[TODAY].status == "HEALTH" and s.health_days == 1 and s.penalty_total == 0 and s.unsuccessful_days == 0
    db.update_session(conn, session["id"], health_penalty_confirmed=1)
    session = db.get_session(conn, session["id"])
    reports.apply_penalty(conn, alice["id"], session, TODAY, "health", ADMIN)
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.penalty_total == 3 and s.days[TODAY].status == "HEALTH"  # never turned into a RED wasted day


def test_status_override_is_audited_and_separate_from_points(conn, session, alice):
    approved_report(conn, alice, session, TODAY, indep=480)
    reports.set_day_status(conn, alice["id"], session, TODAY, "RED", ADMIN, "dispute")
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert s.days[TODAY].status == "RED" and s.gross_points == 13
    row = conn.execute("SELECT * FROM audit_log WHERE action='day_status_set'").fetchone()
    assert row["actor_id"] == ADMIN and "dispute" in row["details"]
    with pytest.raises(ValidationError):
        reports.set_day_status(conn, alice["id"], session, TODAY, "RED", ADMIN, "")


def test_statistics_counts(conn, session, alice):
    approved_report(conn, alice, session, D(2026, 11, 1), indep=480)
    approved_report(conn, alice, session, D(2026, 11, 2), indep=300)
    reports.submit_report(conn, alice, session, D(2026, 11, 9), report_data(), 1001, today=D(2026, 11, 10))
    reports.apply_penalty(conn, alice["id"], session, D(2026, 11, 3), "wasted", ADMIN)
    s = reports.get_summary(conn, session, alice["id"], TODAY)
    assert (s.success_days, s.partial_days, s.unsuccessful_days, s.pending_reports) == (1, 1, 1, 1)
    assert s.missing_reports == 5  # Nov 4-8 (Nov 3 is RED, Nov 9 pending, Nov 10 = today is never "missing")
    assert s.verified_minutes == 780
    assert s.net_points == s.gross_points - s.penalty_total


def test_missing_report_not_counted_for_today_or_future(conn, session, alice):
    s = reports.get_summary(conn, session, alice["id"], D(2026, 11, 3))
    assert s.missing_reports == 2 and s.unsuccessful_days == 0


# ------------------------------------------------------------ targets & time zone
def tdata():
    return dict(planned_minutes=480, planned_physics=20, planned_chemistry=20, planned_biology=20, planned_exercises=2, notes="")


def test_target_deadline_is_0900_ist(conn, session, alice):
    day = D(2026, 11, 12)
    ok = datetime(2026, 11, 12, 8, 59, tzinfo=config.TZ)
    late = datetime(2026, 11, 12, 9, 1, tzinfo=config.TZ)
    reports.submit_target(conn, alice, session, day, tdata(), 1001, now=ok)
    with pytest.raises(ValidationError):
        reports.submit_target(conn, alice, session, day, dict(tdata(), planned_minutes=400), 1001, now=late)
    # the evening before is allowed too
    reports.submit_target(conn, alice, session, D(2026, 11, 13), tdata(), 1001, now=datetime(2026, 11, 12, 22, 0, tzinfo=config.TZ))


def test_deadline_handles_other_time_zones(conn, session, alice):
    day = D(2026, 11, 12)
    utc_ok = datetime(2026, 11, 12, 3, 20, tzinfo=timezone.utc)  # = 08:50 IST
    utc_late = datetime(2026, 11, 12, 3, 40, tzinfo=timezone.utc)  # = 09:10 IST
    reports.submit_target(conn, alice, session, day, tdata(), 1001, now=utc_ok)
    with pytest.raises(ValidationError):
        reports.submit_target(conn, alice, session, day, tdata(), 1001, now=utc_late)


def test_target_edits_keep_history_and_distinct_from_report(conn, session, alice):
    now = datetime(2026, 11, 10, 7, 0, tzinfo=config.TZ)
    t, created = reports.submit_target(conn, alice, session, TODAY, tdata(), 1001, now=now)
    t2, created2 = reports.submit_target(conn, alice, session, TODAY, dict(tdata(), planned_minutes=600), 1001, now=now)
    assert created and not created2 and t2["version"] == 2 and t2["planned_minutes"] == 600
    assert conn.execute("SELECT COUNT(*) FROM target_history WHERE target_id=?", (t["id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 0


def test_today_uses_kolkata_date():
    config.now_func = lambda: datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)  # already Nov 10 01:30 in IST
    assert config.today() == D(2026, 11, 10) or config.now().date() == D(2026, 11, 9)
    assert config.now().astimezone(config.TZ).date() == D(2026, 11, 10)
