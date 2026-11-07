import sqlite3

import pytest

import config
import database as db
import participants as ppl
import reports
from conftest import ADMIN, approved_report
from utils import ValidationError


def make_legacy(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE p(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT);
    CREATE TABLE l(pid INTEGER, day TEXT, hours REAL, ex INTEGER, subj INTEGER, status TEXT, PRIMARY KEY(pid, day));
    INSERT INTO p(name,type) VALUES('Asha','School-going'),('Ravi','Dummy');
    INSERT INTO l VALUES(1,'2026-11-01',8.0,2,3,'ok'),(1,'2026-11-02',5.5,1,1,'ok'),(1,'2026-11-03',0,0,0,'wasted'),
                        (2,'2026-11-01',7.8,0,0,'ok'),(2,'2026-11-02',0,0,0,'health');
    """)
    c.commit()
    c.close()


def test_new_database_has_schema_session_and_fk(conn):
    assert db.schema_version(conn) == 1
    s = db.get_active_session(conn)
    assert (s["name"], s["session_number"], s["start_date"], s["end_date"]) == ("WINTER ARC", 2, "2026-11-01", "2026-11-30")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO session_members(session_id, participant_id, enrolled_at) VALUES(1, 999, 'x')")


def test_legacy_database_migration_keeps_everything(tmp_path):
    path = tmp_path / "arc.db"
    make_legacy(path)
    conn = db.open_database(path, tmp_path / "bk")
    assert list((tmp_path / "bk").glob("arc-pre-migration-*.db")), "backup must exist before migrating"
    session = db.get_active_session(conn)
    names = [m["display_name"] for m in ppl.list_members(conn, session["id"])]
    assert names == ["Asha", "Ravi"]
    asha = reports.get_summary(conn, session, 1, config.today())
    # day1 = 8+2+3 = 13; day2 5h30 = 330 min -> 5 credited +1 +1 = 7 (partial -2); day3 wasted -5
    assert asha.gross_points == 13 + (5 + 1 + 1)
    assert asha.penalty_total == 2 + 5
    assert asha.verified_minutes == 480 + 330
    ravi = reports.get_summary(conn, session, 2, config.today())
    assert ravi.days[reports.parse_date("2026-11-01")].status == "HALF"  # 7.8h = 468 min: 2 min short even with tolerance
    assert ravi.days[reports.parse_date("2026-11-02")].status == "HEALTH"
    assert conn.execute("SELECT COUNT(*) FROM legacy_l").fetchone()[0] == 5  # old tables are kept
    assert conn.execute("SELECT COUNT(*) FROM legacy_p").fetchone()[0] == 2
    conn.close()


def test_migrate_twice_is_a_noop(tmp_path):
    c = db.open_database(tmp_path / "a.db", tmp_path / "bk")
    s = db.get_active_session(c)
    ppl.register(c, "Keep Me", "dummy", s["id"], telegram_user_id=5)
    assert db.migrate(c, tmp_path / "bk") == 1
    assert c.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1


def test_future_migration_preserves_records_and_backs_up(tmp_path):
    c = db.open_database(tmp_path / "a.db", tmp_path / "bk")
    s = db.get_active_session(c)
    p = ppl.register(c, "Keep Me", "dummy", s["id"], telegram_user_id=5)
    approved_report(c, p, s, config.today())

    def v2(conn):
        conn.execute("ALTER TABLE participants ADD COLUMN nickname TEXT")

    migrations = db.MIGRATIONS + [(2, v2)]
    assert db.migrate(c, tmp_path / "bk", migrations) == 2
    assert c.execute("SELECT display_name, nickname FROM participants").fetchone()["display_name"] == "Keep Me"
    assert c.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1
    assert list((tmp_path / "bk").glob("arc-pre-migration-v1-*.db"))


def test_failed_migration_rolls_back(tmp_path):
    c = db.open_database(tmp_path / "a.db", tmp_path / "bk")

    def bad(conn):
        conn.execute("ALTER TABLE participants ADD COLUMN x TEXT")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db.migrate(c, tmp_path / "bk", db.MIGRATIONS + [(2, bad)])
    assert db.schema_version(c) == 1
    assert "x" not in [r["name"] for r in c.execute("PRAGMA table_info(participants)")]


def test_transaction_rolls_back_whole_multistep_update(conn, session, alice):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("UPDATE participants SET display_name='Changed' WHERE id=?", (alice["id"],))
            raise RuntimeError("fail midway")
    assert ppl.get(conn, alice["id"])["display_name"] == "Alice"


def test_confirmation_tokens_are_single_use_expiring_and_bound_to_admin(conn):
    t = db.create_pending_action(conn, ADMIN, "x", {"a": 1}, ttl_seconds=60)
    assert db.consume_pending_action(conn, t, 999) is None  # someone else
    assert db.consume_pending_action(conn, t, ADMIN) == ("x", {"a": 1})
    assert db.consume_pending_action(conn, t, ADMIN) is None  # repeat press
    t2 = db.create_pending_action(conn, ADMIN, "x", {}, ttl_seconds=-5)
    assert db.consume_pending_action(conn, t2, ADMIN) is None  # expired


def test_sessions_are_independent_and_archive_keeps_data(conn, session, alice):
    approved_report(conn, alice, session, config.today())
    new = db.create_session(conn, "WINTER ARC", 3, "THE OG ASSOCIATION", "2026-12-01", "2026-12-31", copy_rules_from=session["id"])
    ppl.enroll(conn, new, alice["id"])
    s3 = db.get_session(conn, new)
    assert db.get_active_session(conn)["id"] == new and s3["full_green_minutes"] == 480
    assert reports.get_summary(conn, s3, alice["id"], reports.parse_date("2026-12-05")).gross_points == 0
    old = db.get_session(conn, session["id"])
    assert reports.get_summary(conn, old, alice["id"], config.today()).gross_points == 13
    db.archive_session(conn, new)
    assert db.get_session(conn, new)["archived"] == 1


def test_participant_deactivation_keeps_history(conn, session, alice):
    approved_report(conn, alice, session, config.today())
    ppl.set_active(conn, alice["id"], False, ADMIN)
    assert [m["id"] for m in ppl.list_members(conn, session["id"])] == []
    s = reports.get_summary(conn, session, alice["id"], config.today())
    assert s.gross_points == 13 and not s.active
    with pytest.raises(ValidationError):
        reports.submit_report(conn, ppl.get(conn, alice["id"]), session, config.today(), dict(independent_minutes=1), 1001)
    ppl.set_active(conn, alice["id"], True, ADMIN)
    assert len(ppl.list_members(conn, session["id"])) == 1


def test_session_removal_and_reenrollment_keep_records(conn, session, alice):
    approved_report(conn, alice, session, config.today())
    ppl.remove_from_session(conn, session["id"], alice["id"])
    assert not ppl.is_member(conn, session["id"], alice["id"])
    ppl.enroll(conn, session["id"], alice["id"])
    assert reports.get_summary(conn, session, alice["id"], config.today()).gross_points == 13


def test_register_is_idempotent_and_username_not_identity(conn, session):
    a = ppl.register(conn, "Zed", "dummy", session["id"], telegram_user_id=77, username="old")
    b = ppl.register(conn, "Zed", "dummy", session["id"], telegram_user_id=77, username="new")
    assert a["id"] == b["id"]
    assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1


def test_admin_can_register_participant_without_telegram_id(conn, session):
    p = ppl.register(conn, "No Telegram", "school", session["id"], registered_by=ADMIN)
    assert p["telegram_user_id"] is None
    rep = reports.submit_report(conn, p, session, config.today(), dict(independent_minutes=480), ADMIN, by_admin=True)
    assert rep["status"] == "approved"
    with pytest.raises(ValidationError):  # nobody can impersonate an unlinked participant
        reports.submit_report(conn, p, session, config.today(), dict(independent_minutes=1), 12345)
