import asyncio
import io
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

import admin
import bot
import chart
import config
import dashboard
import database as db
import export
import leaderboard
import participants as ppl
import reports
import scheduler
import scoring
import utils
from conftest import ADMIN, FAKE_NOW, approved_report, report_data
from utils import ValidationError

TODAY = date(2026, 11, 10)


@pytest.fixture
def populated(conn, session):
    people = [ppl.register(conn, n, "dummy", session["id"], telegram_user_id=3000 + i) for i, n in enumerate(["Cara", "Dan", "Eli", "Fay"])]
    for i, p in enumerate(people):
        for d in range(1, 8):
            approved_report(conn, p, session, date(2026, 11, d), indep=480 - 40 * ((d + i) % 4), ex=i % 3)
    reports.apply_penalty(conn, people[0]["id"], session, date(2026, 11, 9), "wasted", ADMIN)
    return people


def test_leaderboard_chart_dashboard_export_agree(conn, session, populated):
    sm = reports.build_summaries(conn, session, TODAY)
    ranked = leaderboard.rank(sm, "net")
    # chart uses the same ordering and numbers
    pngs = chart.render_attendance(session, sm, FAKE_NOW, page_size=3, scale=1)
    assert len(pngs) == 2 and all(p.startswith(b"\x89PNG") for p in pngs)
    dash = dashboard.dashboard_data(session, sm, TODAY)
    assert dash["net_points"] == sum(s.net_points for _, s in ranked)
    assert dash["top5"][0].participant_id == ranked[0][1].participant_id
    assert dashboard.render_dashboard(session, dash, FAKE_NOW, scale=1).startswith(b"\x89PNG")
    # export reconciles with the leaderboard
    wb = load_workbook(io.BytesIO(export.build_workbook(conn, session, FAKE_NOW, TODAY)))
    assert {"Participants", "Daily Targets", "Daily Reports", "Daily Points", "Penalty Ledger", "Summary", "Attendance"} <= set(wb.sheetnames)
    ws = wb["Summary"]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref
    rows = {r[1].value: r for r in ws.iter_rows(min_row=2) if r[0].value}
    for rank_no, s in ranked:
        r = rows[s.name]
        assert (r[0].value, r[4].value, r[5].value, r[6].value) == (rank_no, s.gross_points, s.penalty_total, s.net_points)
    daily = wb["Daily Points"]
    assert sum(r[7].value for r in daily.iter_rows(min_row=2)) == sum(s.gross_points for s in sm)
    pen = wb["Penalty Ledger"]
    rows = list(pen.iter_rows(min_row=2, values_only=True))
    assert sum(r[4] for r in rows if r[9] == "active") == sum(s.penalty_total for s in sm)  # ledger == leaderboard
    assert sum(1 for r in rows if r[3] == "wasted") == 1


def test_leaderboard_ties_are_deterministic_and_share_rank(conn, session):
    a = ppl.register(conn, "Beta", "dummy", session["id"], telegram_user_id=1)
    b = ppl.register(conn, "Alpha", "dummy", session["id"], telegram_user_id=2)
    for p in (a, b):
        approved_report(conn, p, session, date(2026, 11, 1))
    ranked = leaderboard.standings(conn, session, TODAY)
    assert [(r, s.name) for r, s in ranked] == [(1, "Alpha"), (1, "Beta")]
    assert [(r, s.name) for r, s in leaderboard.standings(conn, session, TODAY)] == [(r, s.name) for r, s in ranked]


def test_leaderboard_text_fits_telegram_limit_with_many_people(conn, session):
    for i in range(60):
        ppl.register(conn, f"<b>Name {i}</b> & co " + "x" * 20, "dummy", session["id"], telegram_user_id=9000 + i)
    ranked = leaderboard.standings(conn, session, TODAY)
    text, page, pages = leaderboard.render(session, ranked, "net", 0)
    assert pages == 6 and len(text) < 4096 and "&lt;b&gt;" in text  # names are HTML-escaped


@pytest.mark.parametrize("kind", list(leaderboard.BOARDS))
def test_every_board_renders(conn, session, populated, kind):
    text, _, _ = leaderboard.render(session, leaderboard.standings(conn, session, TODAY, kind), kind, 0)
    assert "WINTER ARC" in text and "01 NOV 2026" in text


def test_chart_handles_long_names_and_empty_session(conn, session):
    ppl.register(conn, "A" * 40, "dummy", session["id"], telegram_user_id=5)
    assert chart.render_attendance(session, reports.build_summaries(conn, session, TODAY), FAKE_NOW, 22, scale=1)
    empty = db.create_session(conn, "X", 9, "G", "2027-01-01", "2027-01-10")
    s = db.get_session(conn, empty)
    assert chart.render_attendance(s, reports.build_summaries(conn, s, TODAY), FAKE_NOW, 22, scale=1)


# ---------------------------------------------------------------- reminders
class FakeBot:
    def __init__(self, block=()):
        self.sent, self.block = [], set(block)

    async def send_message(self, chat_id, text, **kw):
        from telegram.error import Forbidden
        if chat_id in self.block:
            raise Forbidden("bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


def test_reminders_are_sent_once_and_blocked_users_handled(conn, session):
    ppl.register(conn, "Ok", "dummy", session["id"], telegram_user_id=10)
    ppl.register(conn, "Blocked", "dummy", session["id"], telegram_user_id=11)
    ppl.register(conn, "Manual", "dummy", session["id"])  # no Telegram ID: cannot be messaged
    reports.submit_report(conn, ppl.get_by_telegram(conn, 10), session, TODAY, report_data(), 10)  # Ok already reported today
    fake = FakeBot(block=[11])
    at = datetime(2026, 11, 10, 21, 5, tzinfo=config.TZ)
    first = asyncio.run(scheduler.run_due(conn, fake, {ADMIN}, at))
    assert first["evening"] == 0 and first["admin"] == 1  # Ok skipped (reported); Blocked gets nothing; admin told
    assert [c for c, _ in fake.sent] == [ADMIN]
    again = asyncio.run(scheduler.run_due(conn, fake, {ADMIN}, at))
    assert len(fake.sent) == 1 and again["admin"] == 0  # no duplicates
    assert conn.execute("SELECT status FROM reminders_sent WHERE participant_id=(SELECT id FROM participants WHERE telegram_user_id=11)").fetchone()["status"] == "blocked"


def test_morning_reminder_only_before_deadline_and_toggle(conn, session):
    p = ppl.register(conn, "Early", "dummy", session["id"], telegram_user_id=10)
    fake = FakeBot()
    asyncio.run(scheduler.run_due(conn, fake, set(), datetime(2026, 11, 10, 8, 5, tzinfo=config.TZ)))
    assert len(fake.sent) == 1 and "targets" in fake.sent[0][1]
    db.set_setting(conn, "rem_morning_enabled", "0")
    fake2 = FakeBot()
    conn.execute("DELETE FROM reminders_sent")
    asyncio.run(scheduler.run_due(conn, fake2, set(), datetime(2026, 11, 10, 8, 5, tzinfo=config.TZ)))
    assert fake2.sent == []
    assert not scheduler.due(datetime(2026, 11, 10, 7, 59, tzinfo=config.TZ), "08:00")
    assert not scheduler.due(datetime(2026, 11, 10, 10, 30, tzinfo=config.TZ), "08:00")  # too late to catch up


def test_no_reminders_outside_session_dates(conn, session):
    ppl.register(conn, "Early", "dummy", session["id"], telegram_user_id=10)
    fake = FakeBot()
    asyncio.run(scheduler.run_due(conn, fake, set(), datetime(2026, 12, 5, 21, 5, tzinfo=config.TZ)))
    assert fake.sent == []


# ---------------------------------------------------------------- input parsing, wiring, security
@pytest.mark.parametrize("text,minutes", [("7:30", 450), ("7h30m", 450), ("7h 30", 450), ("7.5h", 450), ("450m", 450), ("8h", 480), ("0", 0), ("90 min", 90)])
def test_parse_duration_ok(text, minutes):
    assert utils.parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["8", "-5m", "abc", "7:75", "25h", "", "1e3h"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValidationError):
        utils.parse_duration(text)


def test_parse_count():
    assert utils.parse_count("15", 100) == 15
    for bad in ("-1", "1.5", "x", "101"):
        with pytest.raises(ValidationError):
            utils.parse_count(bad, 100)


def test_config_validation(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(config.ConfigError):
        config.load_config()
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_IDS", "12, x")
    with pytest.raises(config.ConfigError):
        config.load_config()
    monkeypatch.setenv("ADMIN_IDS", "12, 34")
    assert config.load_config().admin_ids == frozenset({12, 34})


def test_application_builds_and_registers_all_handlers(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN))
    cfg = config.load_config()
    app = bot.build_application(cfg, conn)
    cmds = {c for h in app.handlers[0] if hasattr(h, "commands") for c in h.commands}
    conv = next(h for h in app.handlers[0] if type(h).__name__ == "ConversationHandler")
    cmds |= {c for e in conv.entry_points if hasattr(e, "commands") for c in e.commands}
    assert {"start", "report", "target", "admin", "leaderboard", "chart", "dashboard", "export", "penalty", "backup"} <= cmds
    assert len(app.handlers[1]) == 1 and app.job_queue is not None


class _Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _U:
    def __init__(self, uid):
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"type": "private", "id": uid})()
        self.effective_message = _Msg()
        self.callback_query = None


class _Ctx:
    def __init__(self, conn, admin_ids):
        self.application = type("A", (), {"bot_data": {"conn": conn, "cfg": type("C", (), {"admin_ids": frozenset(admin_ids)})()}})()
        self.args, self.user_data, self.bot = [], {}, None


def test_non_admin_cannot_run_admin_commands(conn, session, alice):
    u = _U(555)
    for fn in (admin.penalty_cmd, admin.addp_cmd, admin.export_cmd, admin.backup_cmd, admin.logreport_cmd):
        asyncio.run(fn(u, _Ctx(conn, {ADMIN})))
    assert all("admins only" in r for r in u.effective_message.replies) and len(u.effective_message.replies) == 5
    assert conn.execute("SELECT COUNT(*) FROM penalties").fetchone()[0] == 0


def test_non_admin_cannot_use_admin_buttons_or_confirmations(conn, session, alice):
    answers = []

    class Q:
        data = "a:pc:1:wasted:2026-11-10"

        async def answer(self, text=None, show_alert=False):
            answers.append(text)

    u = _U(555)
    u.callback_query = Q()
    asyncio.run(admin.admin_callback(u, _Ctx(conn, {ADMIN})))
    token = db.create_pending_action(conn, ADMIN, "apply_penalty", {"pid": alice["id"], "kind": "wasted", "date": "2026-11-10", "session_id": session["id"]})
    Q.data = f"cf:y:{token}"
    asyncio.run(admin.confirm_callback(u, _Ctx(conn, {ADMIN})))
    assert answers == ["⛔ Admins only.", "⛔ Admins only."]
    assert conn.execute("SELECT COUNT(*) FROM penalties").fetchone()[0] == 0
    assert db.consume_pending_action(conn, token, ADMIN) is not None  # token untouched by the intruder


def test_log_redacts_token():
    f = bot.RedactingFormatter("SECRET:TOKEN", "%(message)s")
    import logging
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "GET https://api.telegram.org/botSECRET:TOKEN/getMe failed", None, None)
    assert "SECRET:TOKEN" not in f.format(rec)
