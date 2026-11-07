"""The website is tested against a real HTTP server on a temporary port, reading the same database file the bot uses."""
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date

import pytest

import config
import database as db
import participants as ppl
import reports
import scheduler
import web
from conftest import ADMIN, approved_report

PASSWORD = "correct horse battery"
TODAY = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(web, "CACHE_SECONDS", 0)
    web._attempts.clear()


@pytest.fixture
def site(conn, session, tmp_path):
    people = [ppl.register(conn, n, "dummy", session["id"], telegram_user_id=7000 + i) for i, n in enumerate(["Aki", "<script>alert(1)</script>", "Yuki"])]
    for i, p in enumerate(people):
        for d in range(1, 8):
            approved_report(conn, p, session, date(2026, 11, d), indep=480 - 60 * ((d + i) % 3), notes="SECRET-NOTE")
    cfg = config.Config(bot_token="", admin_ids=frozenset({ADMIN}), db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk", log_level="INFO", font_path="", run_mode="web", port=0, web_admin_password=PASSWORD, web_secret="s3cret")
    server = web.WebServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield type("S", (), {"base": base, "cfg": cfg, "people": people, "server": server})
    server.shutdown()
    server.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, cookie=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def text(r):
    return r[1].decode()


def test_public_pages_render(site):
    for path in ["/", "/leaderboard", "/leaderboard?kind=hours", "/leaderboard?kind=streak", "/leaderboard?kind=days", "/leaderboard?kind=avg",
                 "/leaderboard?page=9", "/streaks", "/stats", "/calendar", f"/calendar?p={site.people[0]['id']}", f"/participant/{site.people[0]['id']}", "/healthz"]:
        status, body, headers = get(site.base + path)
        assert status == 200, path
    home = text(get(site.base + "/"))
    assert "WINTER ARC" in home.upper() and "Aki" in home and "Session 02" in home
    assert "November 2026" in text(get(site.base + f"/participant/{site.people[0]['id']}"))


def test_site_shows_the_same_database_live(site, conn, session):
    new = ppl.register(conn, "Brand New Person", "dummy", session["id"], telegram_user_id=1)
    assert "Brand New Person" in text(get(site.base + "/leaderboard"))
    approved_report(conn, new, session, date(2026, 11, 1))
    page = text(get(site.base + f"/participant/{new['id']}"))
    assert "Net points" in page and ">13<" in page  # 8h + 2 exercise + 3 subjects


def test_names_are_escaped_and_private_data_is_not_public(site):
    pages = "".join(text(get(site.base + p)) for p in ["/", "/leaderboard", "/calendar", "/stats", f"/participant/{site.people[1]['id']}"])
    assert "<script>alert(1)" not in pages and "&lt;script&gt;" in pages
    assert "SECRET-NOTE" not in pages and "7000" not in pages  # no notes, no Telegram IDs


def test_security_headers(site):
    headers = get(site.base + "/")[2]
    csp = headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY" and "default-src 'none'" in csp and "script-src 'self'" in csp
    import re
    page = text(get(site.base + "/"))
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "inline scripts would be blocked by the policy"
    assert not re.search(r"\son[a-z]+=", page) and "javascript:" not in page


def test_unknown_pages_404(site):
    assert get(site.base + "/nope")[0] == 404
    assert get(site.base + "/participant/99999")[0] == 404
    assert get(site.base + "/participant/abc")[0] == 404


def login(site, password=PASSWORD):
    status, body, headers = get(site.base + "/admin/login", data=f"password={password}".encode())
    return status, headers.get("Set-Cookie", "").split(";")[0]


def test_admin_requires_login(site):
    assert get(site.base + "/admin")[0] == 302
    assert get(site.base + "/admin/export.xlsx")[0] == 302
    assert get(site.base + "/admin", cookie="wa_admin=1.deadbeef")[0] == 302  # forged cookie
    assert login(site, "wrong")[0] == 401


def test_admin_dashboard_and_export(site, conn, session):
    reports.submit_report(conn, site.people[0], session, TODAY, dict(independent_minutes=300), 7000)  # pending
    status, cookie = login(site)
    assert status == 302 and cookie.startswith("wa_admin=")
    page = text(get(site.base + "/admin", cookie=cookie))
    assert "Pending approval" in page and "Aki" in page and "Penalty ledger" in page and "Recent admin activity" in page
    assert "SECRET-NOTE" not in page and "s3cret" not in page and PASSWORD not in page
    status, body, headers = get(site.base + "/admin/export.xlsx", cookie=cookie)
    assert status == 200 and body[:2] == b"PK" and "attachment" in headers["Content-Disposition"]
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='web_export'").fetchone()[0] == 1
    assert get(site.base + "/admin", cookie=cookie)[0] == 200
    out = get(site.base + "/admin/logout", cookie=cookie, data=b"")
    assert out[0] == 302 and "Max-Age=0" in out[2]["Set-Cookie"]


def test_login_is_rate_limited(site):
    codes = [login(site, "nope")[0] for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    assert login(site)[0] == 429  # even the right password is refused while locked


def test_admin_disabled_without_password(site):
    site.server.cfg = replace(site.cfg, web_admin_password="")
    assert get(site.base + "/admin")[0] == 302
    assert login(site)[0] == 403


def test_web_connection_is_read_only(site):
    ctx = web.Ctx(site.cfg, {})
    with pytest.raises(sqlite3.OperationalError):
        ctx.conn.execute("DELETE FROM participants")
    ctx.close()
    assert conn_count(site.cfg) == 3


def conn_count(cfg):
    c = sqlite3.connect(cfg.db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
    finally:
        c.close()


def test_archived_session_viewable_with_s_parameter(site, conn, session):
    db.create_session(conn, "WINTER ARC", 3, "THE OG ASSOCIATION", "2026-12-01", "2026-12-31", copy_rules_from=session["id"])
    assert "SESSION 03" in text(get(site.base + "/")).upper()
    old = text(get(site.base + f"/?s={session['id']}"))
    assert "SESSION 02" in old.upper() and "Aki" in old


# ------------------------------------------------------------------ deployment configuration
def test_render_environment_is_detected(monkeypatch):
    for k in ("DB_PATH", "BACKUP_DIR", "ADMIN_IDS", "WEB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:abc")
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://arc.onrender.com")
    cfg = config.load_config()
    assert cfg.on_render and cfg.port == 10000 and cfg.public_url == "https://arc.onrender.com"
    assert cfg.db_path.name == "arc.db" and cfg.backup_dir.name == "backups"


def test_warnings_when_admin_ids_or_password_missing(monkeypatch):
    for k in ("DB_PATH", "WEB_ADMIN_PASSWORD", "ADMIN_IDS", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:abc")
    w = " ".join(config.deploy_warnings(config.load_config()))
    assert "WEB_ADMIN_PASSWORD" in w and "ADMIN_IDS" in w and "ERASED" not in w
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "short")
    assert "shorter than 8" in " ".join(config.deploy_warnings(config.load_config()))


def test_config_modes_and_port_validation(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("RUN_MODE", "web")
    assert config.load_config().run_mode == "web"  # website-only needs no Telegram token
    monkeypatch.setenv("RUN_MODE", "weird")
    with pytest.raises(config.ConfigError):
        config.load_config()
    monkeypatch.setenv("RUN_MODE", "bot")
    monkeypatch.setenv("PORT", "abc")
    monkeypatch.setenv("BOT_TOKEN", "1:abc")
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_daily_backup_files_are_pruned_but_others_kept(conn, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir(exist_ok=True)
    for i in range(20):
        (bk / f"arc-daily-202611{i + 1:02d}-030000.db").write_bytes(b"x")
    (bk / "arc-manual-20261101-000000.db").write_bytes(b"x")
    (bk / "arc-pre-migration-v1-20261101-000000.db").write_bytes(b"x")
    assert scheduler.prune_backups(bk, keep=14) == 6
    assert len(list(bk.glob("arc-daily-*.db"))) == 14
    assert (bk / "arc-manual-20261101-000000.db").exists() and (bk / "arc-pre-migration-v1-20261101-000000.db").exists()
    assert (bk / "arc-daily-20261120-030000.db").exists() and not (bk / "arc-daily-20261101-030000.db").exists()
    path = db.backup_database(conn, bk, "daily")
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1  # the backup is a readable database
    check.close()


def test_project_files_needed_for_deployment_are_present():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert not (root / "railway.json").exists() and not (root / "Procfile").exists()   # old hosting files are gone
    assert "pytest" not in (root / "requirements.txt").read_text()
    assert "python-telegram-bot" in (root / "requirements.txt").read_text()
    for f in ("run.py", "web.py", "web_static/app.css", "web_static/app.js", "web_static/boot.js", "web_static/arc-latin.woff",
              "fonts/DejaVuSans.ttf", "fonts/DejaVuSans-Bold.ttf", ".env.example", ".gitignore", "README.md", "DEPLOY-RENDER.md",
              "PERSISTENCE.md", "KNOWN_LIMITATIONS.md", "CHANGES.md", "FINAL_CHECKLIST.md"):
        assert (root / f).exists(), f
    import run  # noqa: F401  (imports cleanly)
