"""Essential tests for the Turso layer: failures, reconnects, interrupted transactions, concurrency, backup + restore, secrets.
They run on a LOCAL libSQL file through the same RemoteConnection code Turso uses. They do NOT prove real network connectivity."""
import ast
import asyncio
import gzip
import logging
import pathlib
import sqlite3
import threading
from datetime import timedelta

import libsql
import pytest
from telegram.error import BadRequest, Forbidden

import bot
import config
import database as db
import dbconn
import participants as ppl
import reports
import scheduler
import tg
from conftest import ADMIN, FAKE_NOW, approved_report, report_data
from test_e2e_handlers import USER, harness, run  # noqa: F401  (fixture + helper)
from utils import ValidationError

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = FAKE_NOW.date()


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    tg.limiter._last.clear()   # the bot's per-user throttle is process-global; keep tests independent
    yield


class Ctl:
    """Remote control for failure injection; shared by every (re)connection."""
    def __init__(self):
        self.fail_when = self.fail_after = None
        self.times = 0
        self.calls = []


class Flaky:
    def __init__(self, raw, ctl):
        self.raw, self.ctl = raw, ctl

    def execute(self, sql, params=()):
        self.ctl.calls.append(sql.strip())
        if self.ctl.fail_when and self.ctl.times > 0 and self.ctl.fail_when(sql):
            self.ctl.times -= 1
            raise ValueError("stream expired: connection reset by peer")
        cur = self.raw.execute(sql, params)
        if self.ctl.fail_after and self.ctl.times > 0 and self.ctl.fail_after(sql):
            self.ctl.times -= 1
            raise ValueError("connection closed before the response arrived")
        return cur

    def close(self):
        self.raw.close()


def new_remote(path, ctl=None, shared=False, secrets=()):
    ctl = ctl or Ctl()

    def open_raw():
        raw = libsql.connect(str(path), isolation_level=None)
        raw.execute("PRAGMA journal_mode=WAL")
        return Flaky(raw, ctl)
    return dbconn.RemoteConnection(open_raw, sleep=lambda s: None, shared=shared, secrets=secrets, label="test"), ctl


@pytest.fixture
def flaky(tmp_path):
    rc, ctl = new_remote(tmp_path / "turso.db", shared=True)
    db.configure_backend_factory(lambda path: rc)        # bot, website and scheduler all receive THIS object
    conn = db.open_database(tmp_path / "ignored.db", tmp_path / "bk")
    assert conn is rc
    yield conn, ctl
    db.reset_backend()


def sid(conn):
    return db.get_active_session(conn)["id"]


def person(conn, name="Alice", tg_id=1001):
    return ppl.register(conn, name, "dummy", sid(conn), telegram_user_id=tg_id)


# ------------------------------------------------------------------ rows, errors, failures
def test_rows_behave_like_sqlite_rows(flaky):
    conn, _ = flaky
    row = conn.execute("SELECT 5 AS Alpha, 'x' AS beta").fetchone()
    assert (row[0], row["alpha"], row["BETA"], row.keys(), row[0:2], dict(row)) == (5, 5, "x", ["Alpha", "beta"], (5, "x"), {"Alpha": 5, "beta": "x"})
    with pytest.raises(IndexError):
        row["nope"]


def test_constraint_errors_are_integrity_errors(flaky):
    conn, _ = flaky
    person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO session_members(session_id, participant_id, enrolled_at) VALUES(1, 999, 'x')")  # foreign key
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM table_that_does_not_exist")


def test_reads_reconnect_and_retry(flaky):
    conn, ctl = flaky
    before = conn.reconnects
    ctl.fail_when, ctl.times = (lambda s: s.lstrip().upper().startswith("SELECT")), 2
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert conn.reconnects >= before + 2


def test_writes_are_never_retried_and_the_connection_recovers(flaky):
    conn, ctl = flaky
    conn.execute("CREATE TABLE w_t(v)")
    ctl.fail_when, ctl.times = (lambda s: s.startswith("INSERT INTO w_t")), 1
    with pytest.raises(dbconn.DatabaseUnavailable):
        conn.execute("INSERT INTO w_t VALUES('a')")
    assert sum(1 for c in ctl.calls if c.startswith("INSERT INTO w_t")) == 1   # tried exactly once
    assert conn.execute("SELECT COUNT(*) FROM w_t").fetchone()[0] == 0
    conn.execute("INSERT INTO w_t VALUES('a')")
    assert conn.execute("SELECT COUNT(*) FROM w_t").fetchone()[0] == 1


def test_interrupted_transaction_leaves_nothing_behind(flaky):
    conn, ctl = flaky
    conn.execute("CREATE TABLE tx_t(v)")
    ctl.fail_when, ctl.times = (lambda s: "tx2" in s), 1
    with pytest.raises(dbconn.DatabaseUnavailable) as err:
        with db.transaction(conn):
            conn.execute("INSERT INTO tx_t VALUES('tx1')")
            conn.execute("INSERT INTO tx_t VALUES('tx2')")
    assert err.value.during_transaction and not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM tx_t").fetchone()[0] == 0


def test_commit_after_explicit_connection_drop_is_an_interrupted_transaction(tmp_path):
    path = tmp_path / "dropped.db"
    conn = dbconn.RemoteConnection(lambda: sqlite3.connect(path, isolation_level=None), sleep=lambda _: None)
    other = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE tx_t(v)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO tx_t VALUES('uncommitted')")
        conn._drop()  # the original transaction disappears with its connection
        with pytest.raises(dbconn.DatabaseUnavailable) as err:
            conn.execute("COMMIT")
        assert err.value.during_transaction and not err.value.ambiguous
        assert not conn.in_transaction
        assert other.execute("SELECT COUNT(*) FROM tx_t").fetchone()[0] == 0
        assert conn.execute("SELECT 1").fetchone()[0] == 1  # a later operation reconnects
    finally:
        other.close()
        conn.shutdown()


def test_ambiguous_commit_then_retry_creates_no_duplicate_report(flaky):
    conn, ctl = flaky
    alice, session = person(conn), db.get_active_session(conn)
    ctl.fail_after, ctl.times = (lambda s: s.strip().upper() == "COMMIT"), 1
    with pytest.raises(dbconn.DatabaseUnavailable) as err:
        reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)
    assert err.value.ambiguous                                    # the answer was lost: the report may or may not exist
    assert reports.get_report(conn, alice["id"], session["id"], TODAY) is not None   # here it WAS saved
    reports.submit_report(conn, alice, session, TODAY, report_data(), 1001)           # the participant retries
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1     # still exactly one report


def test_startup_never_falls_back_to_a_local_sqlite_file(tmp_path):
    rc = dbconn.RemoteConnection(lambda: (_ for _ in ()).throw(ValueError("connection refused")), sleep=lambda s: None)
    db.configure_backend_factory(lambda path: rc)
    try:
        with pytest.raises(dbconn.DatabaseUnavailable):
            db.open_database(tmp_path / "never.db", tmp_path / "bk")
    finally:
        db.reset_backend()
    assert not list(tmp_path.glob("*.db")) and not (tmp_path / "bk").exists()


def test_wrong_credentials_are_reported_not_retried(tmp_path):
    calls = []
    rc = dbconn.RemoteConnection(lambda: (calls.append(1), (_ for _ in ()).throw(ValueError("HTTP 401 Unauthorized")))[1], sleep=lambda s: None)
    with pytest.raises(dbconn.DatabaseConfigError):
        rc.execute("SELECT 1")
    assert len(calls) == 1


# ------------------------------------------------------------------ concurrency + duplicate prevention
def run_threads(n, fn):
    errors, results = [], []

    def wrap(i):
        try:
            results.append(fn(i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results, errors


def test_transactions_are_isolated_between_threads(flaky):
    conn, _ = flaky
    conn.execute("CREATE TABLE ctr(n)")
    conn.execute("INSERT INTO ctr VALUES(0)")

    def bump(_):
        for _ in range(10):
            with db.transaction(conn):
                n = conn.execute("SELECT n FROM ctr").fetchone()[0]
                conn.execute("UPDATE ctr SET n=?", (n + 1,))
    _, errors = run_threads(6, bump)
    assert not errors and conn.execute("SELECT n FROM ctr").fetchone()[0] == 60       # no lost update
    # a plain write from another thread waits for an open transaction instead of slipping into it
    conn.execute("CREATE TABLE ord(v)")
    started, order = threading.Event(), []

    def holder():
        with db.transaction(conn):
            conn.execute("INSERT INTO ord VALUES('a1')")
            started.set()
            threading.Event().wait(0.3)
            conn.execute("INSERT INTO ord VALUES('a2')")
    t = threading.Thread(target=holder)
    t.start()
    started.wait()
    conn.execute("INSERT INTO ord VALUES('b1')")
    t.join()
    assert [r[0] for r in conn.execute("SELECT v FROM ord ORDER BY rowid").fetchall()] == ["a1", "a2", "b1"]


def test_simultaneous_submissions_cannot_duplicate(flaky):
    conn, _ = flaky
    alice, session = person(conn), db.get_active_session(conn)
    _, errors = run_threads(6, lambda i: reports.submit_report(conn, alice, session, TODAY, report_data(indep=400 + i), 1001))
    assert not errors and conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1
    results, errors = run_threads(6, lambda i: reports.apply_penalty(conn, alice["id"], session, TODAY, "wasted", ADMIN))
    assert len(results) == 1 and all(isinstance(e, ValidationError) for e in errors)  # exactly one penalty
    token = db.create_pending_action(conn, ADMIN, "x", {})
    got, _ = run_threads(6, lambda i: db.consume_pending_action(conn, token, ADMIN))
    assert sum(1 for g in got if g) == 1                                              # a confirmation works exactly once


def test_website_view_is_read_only_and_uses_the_same_connection(flaky):
    conn, _ = flaky
    person(conn)
    view = db.connect_readonly("ignored")
    assert view._conn is conn
    assert view.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1
    for bad in ("INSERT INTO bot_settings VALUES('a','b')", "UPDATE participants SET display_name='x'", "DELETE FROM participants",
                "DROP TABLE participants", "PRAGMA user_version=5", "SELECT 1; DELETE FROM participants",
                "WITH x AS (SELECT 1) DELETE FROM participants"):
        with pytest.raises(sqlite3.OperationalError):
            view.execute(bad)
    view.close()
    assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 1       # the shared connection survived


# ------------------------------------------------------------------ backup + restore (separate test database)
def fill(conn):
    session = db.get_active_session(conn)
    a, b = person(conn, "Alice", 1001), person(conn, "Bob", 1002)
    from datetime import date
    for d in range(1, 8):
        approved_report(conn, a, session, date(2026, 11, d), indep=480 - 30 * (d % 3))
        approved_report(conn, b, session, date(2026, 11, d), indep=300)
    reports.apply_penalty(conn, b["id"], session, date(2026, 11, 9), "wasted", ADMIN)
    return session


def snapshot_view(conn, today=TODAY):
    s = db.get_active_session(conn)
    return {x.name: (x.net_points, x.gross_points, x.penalty_total, x.current_streak, x.longest_streak, x.success_days)
            for x in reports.build_summaries(conn, s, today, only_active=False)}


def test_backup_is_valid_and_restores_into_a_separate_test_database(flaky, tmp_path):
    conn, _ = flaky
    fill(conn)
    snap = db.backup_database(conn, tmp_path / "bk", "t")
    check = sqlite3.connect(snap)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and check.execute("PRAGMA foreign_key_check").fetchall() == []
    assert check.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 14
    check.close()
    gz = dbconn.gzip_file(snap, tmp_path / "bk" / "t.db.gz")
    for source, name in ((snap, "plain"), (gz, "gzipped")):
        target, _ = new_remote(tmp_path / f"test_target_{name}.db")                  # a separate, empty test database
        result = dbconn.restore_from_sqlite(target, source, latest_version=1)
        assert result["tables"]["daily_reports"] == 14
        assert snapshot_view(target) == snapshot_view(conn)                          # identical scoring, penalties and streaks
        assert db.migrate(target, tmp_path / "bk") == 1                              # the restored database starts normally


def test_restore_refuses_unsafe_requests_and_is_all_or_nothing(flaky, tmp_path):
    conn, ctl = flaky
    fill(conn)
    snap = db.backup_database(conn, tmp_path / "bk", "t")
    before = dbconn.table_counts(conn)
    with pytest.raises(dbconn.RestoreRefused, match="already contains"):
        dbconn.restore_from_sqlite(conn, snap)
    with pytest.raises(dbconn.RestoreRefused, match="snapshot"):
        dbconn.restore_from_sqlite(conn, snap, force=True)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 50)
    wrong = tmp_path / "wrong.db"
    sqlite3.connect(wrong).executescript("CREATE TABLE other(x); PRAGMA user_version=1;")
    newer = tmp_path / "newer.db"
    newer.write_bytes(snap.read_bytes())
    c = sqlite3.connect(newer)
    c.execute("PRAGMA user_version=99")
    c.commit()
    c.close()
    for bad in (junk, wrong, newer, tmp_path / "missing.db"):
        with pytest.raises(dbconn.RestoreRefused):
            dbconn.restore_from_sqlite(conn, bad, force=True, snapshot_dir=tmp_path / "snap")
    assert dbconn.table_counts(conn) == before                                        # nothing changed
    # forced restore saves a snapshot of the old data first
    person(conn, "Carol", 1003)
    result = dbconn.restore_from_sqlite(conn, snap, force=True, snapshot_dir=tmp_path / "snap")
    old = sqlite3.connect(result["snapshot"])
    assert old.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 3        # the snapshot holds the data that was replaced
    assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 2
    # a failure in the middle of a restore rolls everything back
    person(conn, "Dave", 1004)
    ctl.fail_when, ctl.times = (lambda s: s.startswith('INSERT INTO "daily_reports"')), 1
    with pytest.raises(dbconn.DatabaseUnavailable):
        dbconn.restore_from_sqlite(conn, snap, force=True, snapshot_dir=tmp_path / "snap")
    assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 3 and conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 14


class FakeBot:
    def __init__(self, mode="ok"):
        self.mode, self.sent = mode, []

    async def send_document(self, chat_id, document, filename=None, caption=None):
        if self.mode == "forbidden":
            raise Forbidden("bot was kicked from the group")
        self.sent.append((chat_id, filename, caption, document.read()))


def backup_cfg(tmp_path, chat=-1001234567890):
    return config.Config(bot_token="", admin_ids=frozenset({ADMIN}), db_path=tmp_path / "x.db", backup_dir=tmp_path / "bk", log_level="INFO",
                         font_path="", backup_chat_id=chat, backup_every_hours=24, backup_keep=30)


def test_offsite_backup_job_sends_restorable_files_retries_and_retains(flaky, tmp_path):
    conn, _ = flaky
    cfg = backup_cfg(tmp_path)
    go = lambda bot_, now, **kw: asyncio.run(scheduler.run_offsite_backup(conn, bot_, cfg, now=now, **kw))  # noqa: E731
    assert go(FakeBot(), FAKE_NOW)["status"] == "empty-skipped"                      # nothing registered: nothing to send
    fill(conn)
    ok = FakeBot()
    assert go(ok, FAKE_NOW)["status"] == "sent"
    chat, name, caption, payload = ok.sent[0]
    assert chat == cfg.backup_chat_id and name.endswith(".db.gz") and "2 participants" in caption
    file = tmp_path / name
    file.write_bytes(payload)                                                        # what the group receives...
    target, _ = new_remote(tmp_path / "from_group.db")
    dbconn.restore_from_sqlite(target, file, latest_version=1)                       # ...really restores
    assert snapshot_view(target) == snapshot_view(conn)
    assert go(ok, FAKE_NOW + timedelta(hours=1))["status"] == "not-due"
    assert go(ok, FAKE_NOW + timedelta(hours=25))["status"] == "sent"
    # failure: recorded, last success unchanged, retried after 30 minutes, error cleared on success
    last_ok = db.get_setting(conn, scheduler.OFFSITE_OK)
    later = FAKE_NOW + timedelta(hours=50)
    assert go(FakeBot("forbidden"), later)["status"] == "failed"
    assert db.get_setting(conn, scheduler.OFFSITE_OK) == last_ok and "not delivered" in db.get_setting(conn, scheduler.OFFSITE_ERR)
    assert go(FakeBot(), later + timedelta(minutes=10))["status"] == "waiting-retry"
    assert go(FakeBot(), later + timedelta(minutes=31))["status"] == "sent" and db.get_setting(conn, scheduler.OFFSITE_ERR) == ""
    # no group configured: the admins receive it
    admin_bot = FakeBot()
    asyncio.run(scheduler.run_offsite_backup(conn, admin_bot, backup_cfg(tmp_path, chat=0), now=later + timedelta(days=3)))
    assert [c for c, *_ in admin_bot.sent] == [ADMIN]
    # retention
    for i in range(40):
        (tmp_path / "bk" / f"winter-arc-backup-202610{i % 28 + 1:02d}{i:02d}-IST.db.gz").write_bytes(b"x")
    scheduler.prune_backups(tmp_path / "bk", keep=30, pattern="winter-arc-backup-*.db.gz")
    assert len(list((tmp_path / "bk").glob("winter-arc-backup-*.db.gz"))) == 30


# ------------------------------------------------------------------ secrets, config, structure
def test_secrets_never_reach_exceptions_or_logs(tmp_path, caplog):
    secret = "SUPERSECRETTOKEN123"
    ctl = Ctl()
    rc, _ = new_remote(tmp_path / "s.db", ctl, secrets=[secret])
    rc.execute("SELECT 1")
    ctl.fail_when, ctl.times = (lambda s: True), 9
    ctl.fail_when = lambda s: s.startswith("SELECT")
    orig = Flaky.execute

    def leaky(self, sql, params=()):
        if sql.startswith("SELECT") and self.ctl.times > 0:
            self.ctl.times -= 1
            raise ValueError(f"connection reset (token {secret})")
        return orig(self, sql, params)
    Flaky.execute = leaky
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(dbconn.DatabaseUnavailable) as err:
            rc.execute("SELECT 2")
    finally:
        Flaky.execute = orig
    assert secret not in str(err.value) and secret not in caplog.text
    fmt = bot.RedactingFormatter(["AAAA-bot-token", "BBBB-turso-token"], "%(message)s")
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "url https://x/botAAAA-bot-token and BBBB-turso-token", None, None)
    out = fmt.format(rec)
    assert "AAAA" not in out and "BBBB" not in out


def test_turso_and_backup_configuration_is_validated(monkeypatch):
    for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "BACKUP_CHAT_ID", "BACKUP_EVERY_HOURS", "BACKUP_KEEP_DAYS", "RENDER", "ADMIN_IDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:abc")
    assert not config.load_config().uses_turso
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://x-y.turso.io")
    with pytest.raises(config.ConfigError, match="BOTH"):
        config.load_config()                                                          # half a setup is refused, never ignored
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("BACKUP_CHAT_ID", "-1001234567890")
    cfg = config.load_config()
    assert cfg.uses_turso and cfg.backup_chat_id == -1001234567890 and cfg.backup_keep == 30 and cfg.backup_every_hours == 24
    assert not any("BACKUP_CHAT_ID" in w for w in config.deploy_warnings(cfg))
    for var, val in (("TURSO_DATABASE_URL", "ftp://nope"), ("BACKUP_CHAT_ID", "abc"), ("BACKUP_EVERY_HOURS", "0")):
        monkeypatch.setenv(var, val)
        with pytest.raises(config.ConfigError):
            config.load_config()
        monkeypatch.undo() if False else None
        monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://x-y.turso.io")
        monkeypatch.setenv("BACKUP_CHAT_ID", "-1001234567890")
        monkeypatch.setenv("BACKUP_EVERY_HOURS", "24")
    monkeypatch.delenv("BACKUP_CHAT_ID")
    assert any("BACKUP_CHAT_ID" in w for w in config.deploy_warnings(config.load_config()))


def test_no_await_inside_a_transaction_block():
    """The lock is per thread: an await inside a transaction could let another coroutine slip statements into it."""
    bad = []
    for p in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.With) and any("transaction" in ast.dump(i.context_expr) for i in node.items):
                bad += [(p.name, node.lineno) for sub in ast.walk(node) if isinstance(sub, (ast.Await, ast.AsyncFor, ast.AsyncWith))]
    assert not bad, bad


# ------------------------------------------------------------------ Telegram side: "Query is too old" and honest failure messages
def test_query_too_old_does_not_stop_the_action(harness, conn):
    h = harness
    orig = h.fake_post

    async def too_old(endpoint, data=None, **kw):
        if endpoint == "answerCallbackQuery":
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        return await orig(endpoint, data, **kw)
    h.fake_post = too_old
    run(h, h.text(USER, "/start"))
    run(h, h.press(USER, "reg:dummy"))                                               # the button answer is refused by Telegram...
    assert conn.execute("SELECT COUNT(*) FROM participants WHERE telegram_user_id=?", (USER,)).fetchone()[0] == 1   # ...but the registration happened
    session = db.get_active_session(conn)
    me = ppl.get_by_telegram(conn, USER)
    rep = reports.submit_report(conn, me, session, TODAY, report_data(), USER, today=TODAY)
    run(h, h.press(ADMIN, f"a:rv:{rep['id']}:ok"))                                   # admin approval with an expired button
    assert reports.get_report_by_id(conn, rep["id"])["status"] == "approved"


async def _unused():  # keeps flake-style linters quiet about asyncio import in some environments
    return None


def drive_report_to_confirm(h):
    run(h, h.text(USER, "/start"))
    run(h, h.press(USER, "reg:dummy"))
    run(h, h.text(USER, "/report"))
    run(h, h.press(USER, f"fd:{config.today().isoformat()}"))
    for answer in ["8h", "2", "pushups", "15", "15", "15", "-"]:
        run(h, h.text(USER, answer))


def last_edit(h):
    return [d for e, d in h.calls if e in ("editMessageText", "sendMessage") and int(d.get("chat_id", 0)) == USER][-1]


def test_failed_submission_tells_the_participant_exactly_what_happened(harness, conn, monkeypatch):
    h = harness
    drive_report_to_confirm(h)
    real = reports.submit_report

    def down(*a, **k):
        raise dbconn.DatabaseUnavailable("down")
    monkeypatch.setattr(reports, "submit_report", down)
    run(h, h.press(USER, "flow:ok"))
    msg = last_edit(h)
    assert "NOT saved" in msg["text"] and "flow:ok" in str(msg.get("reply_markup"))   # clear verdict + a Try-again button
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 0
    monkeypatch.setattr(reports, "submit_report", real)
    run(h, h.press(USER, "flow:ok"))                                                  # "Try again" needs no re-typing
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1 and "Report received" in last_edit(h)["text"]


def test_lost_confirmation_is_reported_as_saved_and_not_duplicated(harness, conn, monkeypatch):
    h = harness
    drive_report_to_confirm(h)
    real = reports.submit_report

    def saves_then_loses_the_answer(*a, **k):
        real(*a, **k)
        raise dbconn.DatabaseUnavailable("answer lost", during_transaction=True, ambiguous=True)
    monkeypatch.setattr(reports, "submit_report", saves_then_loses_the_answer)
    run(h, h.press(USER, "flow:ok"))
    assert "Saved." in last_edit(h)["text"] and "is stored" in last_edit(h)["text"]
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1


def test_bot_website_and_scheduler_share_one_database(flaky, tmp_path):
    """Production topology: one RemoteConnection object serves the bot, the website's read-only view and the scheduler."""
    import web
    conn, _ = flaky
    person(conn, "Shared Person")
    ctx_conn = web.open_ro("ignored")
    assert ctx_conn._conn is conn and "Shared Person" in [r["display_name"] for r in ctx_conn.execute("SELECT display_name FROM participants").fetchall()]
    assert db.describe_backend().startswith("Turso")
