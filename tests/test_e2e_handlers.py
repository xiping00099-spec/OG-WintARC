"""
End-to-end test through the REAL python-telegram-bot dispatcher. Only the network call (Bot._post) is
replaced by a recorder, so handlers, conversation states, callbacks and the database all run for real.
"""
import asyncio
import itertools

import pytest
from telegram import Bot, Update

import bot
import config
import database as db
import reports
from conftest import ADMIN

USER, USER2 = 2001, 2002
_ids = itertools.count(1)


class Harness:
    def __init__(self, app):
        self.app, self.calls = app, []

    async def fake_post(self, endpoint, data=None, **kw):
        self.calls.append((endpoint, dict(data or {})))
        if endpoint == "getMe":
            return {"id": 999, "is_bot": True, "first_name": "WA", "username": "wa_bot"}
        if endpoint in ("sendMessage", "editMessageText", "sendDocument", "sendPhoto"):
            return {"message_id": next(_ids), "date": 1, "chat": {"id": int((data or {}).get("chat_id", 1)), "type": "private"},
                    "text": (data or {}).get("text", "")}
        return True

    async def send(self, update_dict):
        update_dict["update_id"] = next(_ids)
        await self.app.process_update(Update.de_json(update_dict, self.app.bot))

    def text(self, uid, text, chat_type="private"):
        ent = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None
        msg = {"message_id": next(_ids), "date": 1, "chat": {"id": uid, "type": chat_type}, "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"}, "text": text}
        if ent:
            msg["entities"] = ent
        return self.send({"message": msg})

    def press(self, uid, data):
        return self.send({"callback_query": {"id": str(next(_ids)), "chat_instance": "c", "data": data, "from": {"id": uid, "is_bot": False, "first_name": f"U{uid}"},
                                             "message": {"message_id": next(_ids), "date": 1, "chat": {"id": uid, "type": "private"}, "text": "x"}}})

    def sent_to(self, uid):
        return [d.get("text", "") for e, d in self.calls if e in ("sendMessage", "editMessageText") and int(d.get("chat_id", 0)) == uid]

    def alerts(self):
        return [d for e, d in self.calls if e == "answerCallbackQuery" and d.get("show_alert")]


@pytest.fixture
def harness(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN))
    app = bot.build_application(config.load_config(), conn)
    h = Harness(app)
    monkeypatch.setattr(Bot, "_post", lambda self, endpoint, data=None, **kw: h.fake_post(endpoint, data, **kw))

    async def boot():
        await app.initialize()

    h.loop = asyncio.new_event_loop()
    h.loop.run_until_complete(boot())
    yield h
    h.loop.run_until_complete(app.shutdown())
    h.loop.close()


def run(h, coro):
    return h.loop.run_until_complete(coro)


def test_full_participant_and_admin_journey(harness, conn):
    h = harness
    run(h, h.text(USER, "/start"))
    assert any("not registered" in t for t in h.sent_to(USER))
    run(h, h.press(USER, "reg:start"))
    run(h, h.press(USER, "reg:dummy"))
    assert db.get_setting(conn, "chart_page_size") == "22"
    me = conn.execute("SELECT * FROM participants WHERE telegram_user_id=?", (USER,)).fetchone()
    assert me and me["school_type"] == "dummy"

    # invalid input is explained and the same question is asked again
    run(h, h.text(USER, "/report"))
    run(h, h.press(USER, f"fd:{config.today().isoformat()}"))
    run(h, h.text(USER, "8"))  # ambiguous: no unit
    assert any("couldn't read" in t.lower() for t in h.sent_to(USER))
    for answer in ["8h", "2", "pushups", "15", "15", "15", "-"]:
        run(h, h.text(USER, answer))
    run(h, h.press(USER, "flow:ok"))
    rep = conn.execute("SELECT * FROM daily_reports").fetchone()
    assert rep["status"] == "pending" and rep["independent_minutes"] == 480
    assert any("provisional" in t for t in h.sent_to(USER))
    session = db.get_active_session(conn)
    assert reports.get_summary(conn, session, me["id"], config.today()).gross_points == 0  # pending = no official points

    # pressing the stale confirm button again does nothing harmful
    run(h, h.press(USER, "flow:ok"))
    assert conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0] == 1

    # a normal user cannot reach admin features
    run(h, h.press(USER, f"a:rv:{rep['id']}:ok"))
    run(h, h.text(USER, "/admin"))
    assert h.alerts() and any("admins only" in t.lower() for t in h.sent_to(USER))
    assert conn.execute("SELECT status FROM daily_reports").fetchone()["status"] == "pending"

    # admin approves; participant is notified with the verified breakdown
    run(h, h.text(ADMIN, "/admin"))
    run(h, h.press(ADMIN, "a:pend:0"))
    run(h, h.press(ADMIN, f"a:rv:{rep['id']}:ok"))
    run(h, h.press(ADMIN, f"a:rv:{rep['id']}:ok"))  # repeated press is safe
    assert conn.execute("SELECT status FROM daily_reports").fetchone()["status"] == "approved"
    assert any("Report approved" in t and "Gross points: 13" in t for t in h.sent_to(USER))
    assert reports.get_summary(conn, session, me["id"], config.today()).gross_points == 13

    # penalty needs a confirmation; the token works once
    day = config.today().isoformat()
    run(h, h.press(ADMIN, f"a:pc:{me['id']}:wasted:{day}"))
    assert conn.execute("SELECT COUNT(*) FROM penalties").fetchone()[0] == 0
    token = conn.execute("SELECT token FROM pending_actions").fetchone()["token"]
    run(h, h.press(ADMIN, f"cf:y:{token}"))
    run(h, h.press(ADMIN, f"cf:y:{token}"))
    assert conn.execute("SELECT COUNT(*) FROM penalties WHERE status='active'").fetchone()[0] == 1
    assert reports.get_summary(conn, session, me["id"], config.today()).net_points == 13 - 5

    # admin rejection asks for a typed reason
    other = reports.submit_report(conn, __import__("participants").get(conn, me["id"]), session, config.today() - __import__("datetime").timedelta(days=1),
                                  dict(independent_minutes=100), USER)
    run(h, h.press(ADMIN, f"a:rv:{other['id']}:no"))
    run(h, h.text(ADMIN, "numbers look wrong"))
    assert conn.execute("SELECT status, review_note FROM daily_reports WHERE id=?", (other["id"],)).fetchone()[:] == ("rejected", "numbers look wrong")
    assert any("rejected" in t and "numbers look wrong" in t for t in h.sent_to(USER))

    # public views work for everybody, export is admin only and produces a file
    run(h, h.text(USER, "/leaderboard"))
    assert any("WINTER ARC" in t and "pts" in t for t in h.sent_to(USER))
    run(h, h.text(ADMIN, "/export"))
    assert any(e == "sendDocument" for e, _ in h.calls)
    run(h, h.text(USER, "/chart"))
    assert any(e == "sendPhoto" for e, _ in h.calls)


def test_user_cannot_start_forms_in_group_chat(harness):
    h = harness
    run(h, h.text(USER2, "/report", chat_type="group"))
    assert any("private chat" in t for t in h.sent_to(USER2))


def test_bot_and_website_run_together_on_one_database(harness, conn):
    """RUN_MODE=all in miniature: the real Telegram handlers write, the real website (own connection) reads the same file."""
    import threading
    import urllib.request

    import web
    import pathlib
    cfg = config.Config(bot_token="", admin_ids=frozenset({ADMIN}), db_path=pathlib.Path(conn.execute("PRAGMA database_list").fetchone()["file"]),
                        backup_dir=pathlib.Path("/tmp/unused-bk"), log_level="INFO", font_path="", run_mode="all", port=0)
    server = web.WebServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        web.CACHE_SECONDS = 0
        assert "U4242" not in urllib.request.urlopen(base + "/participants").read().decode()
        run(harness, harness.send({"message": {"message_id": 1, "date": 1, "chat": {"id": 4242, "type": "private"},
                                               "from": {"id": 4242, "is_bot": False, "first_name": "Aki Tester"}, "text": "/start",
                                               "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}))
        run(harness, harness.press(4242, "reg:dummy"))
        assert conn.execute("SELECT COUNT(*) FROM participants WHERE telegram_user_id=4242").fetchone()[0] == 1
        page = urllib.request.urlopen(base + "/participants").read().decode()
        assert "U4242" in page                                         # the bot wrote the participant, the website (separate connection) shows it
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()
