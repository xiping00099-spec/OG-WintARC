"""Starts the website on a THROWAWAY database filled with made-up test data (temp folder). Never touches a real database."""
import sys, random, tempfile, pathlib, threading, time
import os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from datetime import date, datetime
import config, database as db, participants as ppl, reports, web
config.now_func = lambda: datetime(2026, 11, 14, 21, 0, tzinfo=config.TZ)
tmp = pathlib.Path(tempfile.mkdtemp()); conn = db.open_database(tmp/'demo.db', tmp/'bk'); s = db.get_active_session(conn)
random.seed(4)
names = ["Aarav Sharma", "Ishita", "Rohan K.", "Meera Nair", "Zoya", "Karthik Subramaniam Iyer", "Dev", "Ananya"]
for i, n in enumerate(names):
    p = ppl.register(conn, n, "school" if i % 2 else "dummy", s["id"], telegram_user_id=300 + i)
    for k in range(13):
        d = date(2026, 11, 1 + k)
        if random.random() < .08: continue
        reports.submit_report(conn, p, s, d, dict(independent_minutes=random.choice([480, 500, 300, 240, 0]), school_minutes=0, exercise_count=random.randint(0, 3), physics_q=random.choice([0, 15, 20]), chemistry_q=15, biology_q=random.choice([0, 15])), 9, by_admin=True, today=date(2026, 11, 14))
    if i == 2:
        try: reports.apply_penalty(conn, p["id"], s, date(2026, 11, 12), "wasted", 9, today=date(2026, 11, 14))
        except Exception: pass
reports.submit_report(conn, ppl.get(conn, 1), s, date(2026, 11, 14), dict(independent_minutes=200), 300, today=date(2026, 11, 14))  # pending today
reports.set_day_status(conn, 4, s, date(2026, 11, 13), "HEALTH", 9, "rest")
conn.execute("INSERT INTO daily_targets(participant_id, session_id, target_date, planned_minutes, submitted_at, updated_at) VALUES(2,?,?,480,'x','x')", (s["id"], "2026-11-14"))
cfg = config.Config(bot_token="", admin_ids=frozenset({9}), db_path=tmp/'demo.db', backup_dir=tmp/'bk', log_level="INFO", font_path="", run_mode="web", port=0,
                    web_admin_password="demo-password-123", web_secret="k")
srv = web.WebServer(("127.0.0.1", int(os.environ.get("PORT", "18090"))), cfg)
print("READY", flush=True)
srv.serve_forever()
