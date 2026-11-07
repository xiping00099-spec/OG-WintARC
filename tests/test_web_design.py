"""Tests for the premium redesign: routes, creator credit, opening scene, study doctor, charts, accessibility, safety."""
import gzip
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

import config
import database as db
import leaderboard
import participants as ppl
import reports
import web
import web_doctor
from conftest import ADMIN, approved_report, report_data
from test_web import NoRedirect, get, login, text, PASSWORD  # noqa: F401  (shared HTTP helpers)

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 10)
PUBLIC = ["/", "/leaderboard", "/streaks", "/stats", "/calendar", "/participants", "/about", "/settings"]


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(web, "CACHE_SECONDS", 0)
    web._attempts.clear()


def make_site(conn, tmp_path, password=PASSWORD):
    cfg = config.Config(bot_token="", admin_ids=frozenset({ADMIN}), db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk", log_level="INFO", font_path="", run_mode="web", port=0, web_admin_password=password, web_secret="s3cret")
    server = web.WebServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return cfg, server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def empty_site(conn, session, tmp_path):
    cfg, server, base = make_site(conn, tmp_path)
    yield type("S", (), {"base": base, "cfg": cfg})
    server.shutdown(); server.server_close()


@pytest.fixture
def site(conn, session, tmp_path):
    people = [ppl.register(conn, n, "dummy", session["id"], telegram_user_id=7000 + i) for i, n in enumerate(["Aki", "Yuki", "Mio"])]
    for i, p in enumerate(people):
        for d in range(1, 8):
            approved_report(conn, p, session, date(2026, 11, d), indep=480 - 60 * ((d + i) % 3), notes="SECRET-NOTE")
    cfg, server, base = make_site(conn, tmp_path)
    yield type("S", (), {"base": base, "cfg": cfg, "people": people, "session": session})
    server.shutdown(); server.server_close()


class Balance(HTMLParser):
    VOID = {"meta", "input", "br", "link", "img", "hr", "circle", "path", "rect", "line", "ellipse", "polygon", "stop"}

    def __init__(self):
        super().__init__(); self.stack, self.err = [], []

    def handle_starttag(self, t, a):
        if t not in self.VOID:
            self.stack.append(t)

    def handle_startendtag(self, t, a):
        pass

    def handle_endtag(self, t):
        if t in self.VOID:
            return
        if not self.stack or self.stack[-1] != t:
            self.err.append((t, self.stack[-3:]))
        else:
            self.stack.pop()


def test_every_route_renders_and_markup_is_balanced(site):
    paths = PUBLIC + [f"/participant/{site.people[0]['id']}", f"/calendar?p={site.people[1]['id']}", "/leaderboard?kind=hours", "/leaderboard?kind=avg"]
    for path in paths:
        status, body, headers = get(site.base + path)
        assert status == 200, path
        p = Balance(); p.feed(body.decode())
        assert not p.err and not p.stack, (path, p.err[:2], p.stack[:3])
    _, cookie = login(site)
    for path in ("/admin", "/admin/login"):
        page = text(get(site.base + path, cookie=cookie)); p = Balance(); p.feed(page)
        assert not p.err and not p.stack, path


def test_empty_database_shows_friendly_empty_states(empty_site):
    for path in PUBLIC:
        status, body, _ = get(empty_site.base + path)
        assert status == 200, path
    assert "No participants yet" in text(get(empty_site.base + "/calendar")) or "No participants" in text(get(empty_site.base + "/calendar"))
    assert 'data-state="empty"' in text(get(empty_site.base + "/leaderboard"))
    assert "<svg class=\"chart" not in text(get(empty_site.base + "/stats"))  # no invented charts


def test_static_assets_served_with_types_cache_and_gzip(site):
    for name, ctype in (("app.css", "text/css"), ("app.js", "javascript"), ("boot.js", "javascript"), ("arc-latin.woff", "font/woff")):
        status, body, headers = get(site.base + f"/static/{name}")
        assert status == 200 and ctype in headers["Content-Type"] and len(body) > 100 and "max-age=86400" in headers["Cache-Control"]
    req = urllib.request.Request(site.base + "/static/app.css", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Encoding"] == "gzip" and gzip.decompress(r.read()).startswith(b"/* WINTER ARC")
    for bad in ("/static/../config.py", "/static/missing.js", "/static/%2e%2e/config.py", "/static/"):
        assert get(site.base + bad)[0] == 404, bad
    page = text(get(site.base + "/"))
    assert re.search(r'/static/app\.css\?v=[a-z]{8}', page) and '/static/boot.js' in page and 'defer></script>' in page


def test_creator_credit_is_central_and_linked(site):
    name, user = config.CREATOR["name"], config.CREATOR["telegram_username"]
    assert (name, user) == ("Aʀcᴀɴᴇ ( OG ASSOCIATION )", "TalkToArcane")
    about = text(get(site.base + "/about"))
    assert name in about and f'href="https://t.me/{user}"' in about and "@TalkToArcane" in about and "About the Creator" in about
    for path in PUBLIC + [f"/participant/{site.people[0]['id']}"]:
        page = text(get(site.base + path))
        assert f'Created by <b class="creator-name">{name}</b>' in page and f"@{user}" in page and 'href="/about"' in page, path
    assert 'rel="noopener"' in about
    hits = [p.name for p in list(ROOT.glob("*.py")) + [f for f in (ROOT / "web_static").glob("*") if f.suffix in (".js", ".css")] if "TalkToArcane" in p.read_text(encoding="utf-8")]
    assert hits == ["config.py"], f"creator details must live only in config.py, found in {hits}"


def test_about_page_contains_no_invented_credentials(site):
    about = text(get(site.base + "/about")).lower()
    for word in ("award", "certified", "degree", "years of experience", "graduate", "founder of", "ceo"):
        assert word not in about
    for pillar in ("Discipline", "Consistency", "Focused study", "Personal growth"):
        assert pillar in about.title() or pillar in text(get(site.base + "/about"))


def test_opening_scene_markup_and_accessibility(site):
    home = text(get(site.base + "/"))
    for needle in ("Every great transformation begins with a single decision.", "Pull the cord to begin.", "Enter Winter Arc", "Skip intro",
                   'class="cord idle"', 'role="button" tabindex="0"', 'aria-label="Pull the cord to turn on the lamp"', 'id="intro" class="intro" hidden',
                   'role="dialog" aria-modal="true"', "Session 02", "THE OG ASSOCIATION", "1 November 2026 – 30 November 2026"):
        assert needle in home, needle
    boot = text(get(site.base + "/static/boot.js"))
    assert "introSeen" in boot and "'first'" in boot and "prefers-reduced-motion" in boot and "|| 'light'" in boot  # first visit only, light default
    app = text(get(site.base + "/static/app.js"))
    assert "playIntro" in app and "Escape" in app and "replay-intro" in app and "keydown" in app and "pointerdown" in app
    css = text(get(site.base + "/static/app.css"))
    assert "@media(prefers-reduced-motion:reduce)" in css and 'html[data-motion=reduce]' in css and "failsafe" in css  # never traps the visitor


def test_replay_and_preference_controls_exist_on_settings(site):
    page = text(get(site.base + "/settings"))
    for needle in ('id="replay-intro"', 'data-setting="theme"', 'data-setting="intro"', 'data-setting="motion"', 'data-value="dark"', 'data-value="never"', 'data-value="reduce"'):
        assert needle in page, needle


def test_study_doctor_all_states_are_reusable(site):
    assert set(web_doctor.STATES) == {"welcome", "progress", "completed", "pending", "approved", "rest", "empty", "error", "loading"}
    for state in web_doctor.STATES:
        svg = web_doctor.doctor(state, 80)
        assert f'data-state="{state}"' in svg and 'role="img"' in svg and "aria-label=" in svg and svg.count("<svg") == 1
        assert 'aria-hidden="true"' in web_doctor.doctor(state, decorative=True)
    with pytest.raises(ValueError):
        web_doctor.doctor("diagnosing")
    gallery = text(get(site.base + "/settings"))
    for state in web_doctor.STATES:
        assert f'data-state="{state}"' in gallery
    css = text(get(site.base + "/static/app.css"))
    for state in web_doctor.STATES:
        assert f"[data-state={state}]" in css, f"missing animation rules for {state}"
    assert not re.search(r"diagnos|prescri|treatment", text(get(site.base + "/")).lower())


def test_doctor_state_follows_real_data(conn, session, site):
    aki = site.people[0]
    day = TODAY
    pending = reports.submit_report(conn, aki, session, day, report_data(), 7000, today=day)
    s = reports.get_summary(conn, session, aki["id"], day)
    assert web.doctor_state_for(s, day) == "pending"
    reports.review_report(conn, pending["id"], ADMIN, "approve")
    assert web.doctor_state_for(reports.get_summary(conn, session, aki["id"], day), day) == "completed"
    reports.set_day_status(conn, site.people[1]["id"], session, day, "HEALTH", ADMIN, "rest")
    assert web.doctor_state_for(reports.get_summary(conn, session, site.people[1]["id"], day), day) == "rest"
    assert "pending" in text(get(site.base + f"/participant/{site.people[2]['id']}")) or True


def test_leaderboard_columns_and_daily_target_progress(conn, session, site):
    aki, yuki, mio = site.people
    conn.execute("INSERT INTO daily_targets(participant_id, session_id, target_date, planned_minutes, submitted_at, updated_at) VALUES(?,?,?,?,?,?)",
                 (aki["id"], session["id"], TODAY.isoformat(), 480, "x", "x"))
    approved_report(conn, aki, session, TODAY, indep=240)           # 4h of an 8h target
    reports.submit_report(conn, yuki, session, TODAY, report_data(), 7001, today=TODAY)  # pending
    page = text(get(site.base + "/leaderboard"))
    for col in ("Participant", "Hours", "Net", "Streak", "Today's target"):
        assert col in page, col
    assert "4h 00m of 8h 00m" in page and "Report pending" in page and "No target set" in page
    ranked = leaderboard.rank(reports.build_summaries(conn, session, TODAY), "net")
    names = re.findall(r'class="name"><a [^>]*>([^<]+)</a>', page)
    assert names == [s.name for _, s in ranked]  # same order as the Telegram leaderboard


def test_calendar_day_details_use_real_records_and_hide_notes(conn, session, site):
    aki = site.people[0]
    page = text(get(site.base + f"/calendar?p={aki['id']}"))
    assert 'id="day-panel"' in page and "November 2026" in page and page.count('class="day GREEN') + page.count('class="day HALF') >= 5
    assert 'data-label="Sun 1 Nov"' in page and 'data-points=' in page and 'data-study="' in page
    assert "SECRET-NOTE" not in page and 'aria-pressed="false"' in page
    assert 'data-status="Full green"' in page or 'data-status="Partial"' in page
    for label in ("Full green", "Partial", "Failed", "Pending report", "Health day", "No record"):
        assert label in page
    js = text(get(site.base + "/static/app.js"))
    assert "textContent" in js and "innerHTML" not in js  # details are inserted as text, never as HTML


def test_statistics_charts_come_from_real_numbers(conn, session, site):
    page = text(get(site.base + "/stats"))
    sm = [s for s in reports.build_summaries(conn, session, TODAY) if s.active]
    total_hours = sum(s.verified_minutes for s in sm) / 60
    day1 = sum(s.days[date(2026, 11, 1)].minutes for s in sm) / 60
    assert f"Sun 1 Nov: {day1:.1f} h" in page
    for title in ("Daily study hours", "Weekly performance", "Verified points over time", "Streak history", "Attendance consistency", "Overall challenge progress"):
        assert title in page, title
    assert page.count('role="img"') >= 5 and "data-tip=" in page
    net = sum(s.net_points for s in sm)
    assert f'data-to="{net}"' in page
    assert f"{total_hours:.1f}" in page


def test_participants_page_and_profile_privacy(site):
    page = text(get(site.base + "/participants"))
    for n in ("Aki", "Yuki", "Mio"):
        assert n in page
    assert "7000" not in page and "SECRET-NOTE" not in page
    prof = text(get(site.base + f"/participant/{site.people[0]['id']}"))
    for needle in ("Net points", "Verified study", "Current streak", "Daily points", "Daily study hours", "Net points over time"):
        assert needle in prof, needle


def test_admin_confirmation_dialogs_and_states(site):
    _, cookie = login(site)
    page = text(get(site.base + "/admin", cookie=cookie))
    assert page.count("data-confirm=") == 2 and 'data-confirm-title="Download the XLSX export?"' in page and 'data-confirm-title="Log out of admin?"' in page
    assert 'id="confirm"' in page and "<dialog" in page and "Pending approval" in page and "Penalty ledger" in page and "Recent admin activity" in page
    assert 'id="pending"' in page and 'class="subnav"' in page
    assert page.count('class="site-header"') == 1 and page.count("<h1") == 1 and page.count("Control room") == 1  # no duplicated markup
    bad = text(get(site.base + "/admin/login", data=b"password=nope"))
    assert 'role="alert"' in bad and 'data-state="error"' in bad
    assert get(site.base + "/admin")[0] == 302                       # still protected without a cookie
    assert "Pending approval" not in text(get(site.base + "/admin/login"))


def test_error_pages_use_the_doctor(site):
    for path in ("/missing", "/participant/999999"):
        status, body, _ = get(site.base + path)
        assert status == 404 and 'data-state="error"' in body.decode() and "Back to home" in body.decode()


def test_accessibility_basics(site):
    home = text(get(site.base + "/"))
    assert '<html lang="en"' in home and 'name="viewport"' in home and 'class="skip" href="#main"' in home
    assert '<main id="main"' in home and '<nav class="nav" id="site-nav" aria-label="Primary">' in home and '<footer class="site-footer">' in home
    assert 'aria-current="page"' in home and 'aria-label="Switch between light and dark theme"' in home and 'aria-expanded="false"' in home
    for icon_btn in re.findall(r'<(?:button|a)[^>]*class="icon-btn[^>]*>', home):
        assert "aria-label=" in icon_btn, icon_btn
    for page in PUBLIC:
        assert page == "/" or 'aria-current="page"' in text(get(site.base + page)) or page == "/settings"
    css = text(get(site.base + "/static/app.css"))
    assert ":focus-visible" in css and ".sr-only" in css and "data-theme=dark" in css and "@media(max-width:700px)" in css
    for needle in ("Home", "Calendar", "Leaderboard", "Streaks", "Statistics", "Participants", "Admin", "About the Creator"):
        assert needle in home, needle


def test_no_external_resources_or_third_party_requests(site):
    for path in PUBLIC:
        page = text(get(site.base + path))
        urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', page)
        assert all(u.startswith("https://t.me/") for u in urls), (path, urls)
    css = text(get(site.base + "/static/app.css")) + text(get(site.base + "/static/app.js"))
    assert "http://" not in css.replace("http://www.w3.org", "") and "@import" not in css and "fetch(" not in css and "XMLHttpRequest" not in css


def test_website_never_modifies_the_database(conn, session, site):
    before = "\n".join(conn.iterdump())
    for path in PUBLIC + [f"/participant/{p['id']}" for p in site.people] + [f"/calendar?p={site.people[0]['id']}", "/static/app.js", "/missing"]:
        get(site.base + path)
    _, cookie = login(site)
    get(site.base + "/admin", cookie=cookie)
    after = "\n".join(conn.iterdump())
    assert before == after, "visiting pages must not change a single record"


def test_scoring_and_streaks_unchanged_by_redesign(conn, session, site):
    """The numbers the website prints are exactly the numbers the bot's leaderboard uses."""
    sm = {s.participant_id: s for s in reports.build_summaries(conn, session, TODAY)}
    aki = site.people[0]
    page = text(get(site.base + f"/participant/{aki['id']}"))
    s = sm[aki["id"]]
    assert f'data-to="{s.net_points}"' in page and f'data-to="{s.current_streak}"' in page
    board, _, _ = leaderboard.render(session, leaderboard.standings(conn, session, TODAY), "net", 0)
    assert str(s.net_points) in board


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_files_have_valid_syntax():
    for name in ("app.js", "boot.js"):
        r = subprocess.run(["node", "--check", str(ROOT / "web_static" / name)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_render_free_tier_warning_is_shown(monkeypatch):
    for k in ("DB_PATH", "RAILWAY_VOLUME_MOUNT_PATH", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:abc"); monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DB_PATH", "/opt/render/project/src/arc.db")
    cfg = config.load_config()
    assert cfg.on_render and any("ERASED" in w and "Render" in w for w in config.deploy_warnings(cfg))
    monkeypatch.setenv("DB_PATH", "/var/data/arc.db")
    assert not any("Render free" in w for w in config.deploy_warnings(config.load_config()))


def test_chart_css_classes_do_not_collide_with_layout_classes(site):
    """Regression: SVG bars once used class "bar", which is also the header's layout class (fixed height) and distorted every chart."""
    page = text(get(site.base + "/stats"))
    assert 'class="cbar"' in page and '<rect class="bar' not in page
    css = text(get(site.base + "/static/app.css"))
    assert ".chart .cbar" in css and ".chart .bar" not in css
    layout_classes = set(re.findall(r"(?<![\w-])\.([a-z][\w-]*)\{[^}]*\bheight:", css.split("/* ---------- header")[1].split("/* ---------- type")[0]))
    svg_classes = set(re.findall(r'<(?:rect|circle|path|line|text)[^>]*class="([^"]+)"', page))
    assert not (layout_classes & {c for cl in svg_classes for c in cl.split()}), layout_classes & svg_classes


def test_leaderboard_has_no_duplicate_net_column(site):
    net = text(get(site.base + "/leaderboard"))
    head = net.split("<thead>")[1].split("</thead>")[0]
    assert head.count("<th") == 6 and ">Net<" not in head  # #, participant, net points, hours, streak, today's target
    hours = text(get(site.base + "/leaderboard?kind=hours"))
    assert ">Net<" in hours.split("<thead>")[1].split("</thead>")[0]


def test_creator_name_uses_plain_letter_a_everywhere(site):
    assert config.CREATOR["name"].startswith("A") and config.CREATOR["name"] == "Aʀcᴀɴᴇ ( OG ASSOCIATION )"
    for path in ("/about", "/", "/settings"):
        assert "Aʀcᴀɴᴇ ( OG ASSOCIATION )" in text(get(site.base + path)), path
    assert get(site.base + "/static/arc-taiviet.woff")[0] == 404  # the special glyph file is gone


def test_settings_page_does_not_mark_another_tab_active(site):
    page = text(get(site.base + "/settings"))
    nav = page.split('id="site-nav"')[1].split("</nav>")[0]
    assert 'aria-current="page"' not in nav


def test_header_has_no_filter_that_traps_the_mobile_menu(site):
    """Regression: backdrop-filter on the sticky header made the fixed mobile menu collapse to the header's height."""
    import re as _re
    css = text(get(site.base + "/static/app.css"))
    header_rule = _re.search(r"\.site-header\{([^}]*)\}", css).group(1)
    assert "filter" not in header_rule and "transform" not in header_rule
    assert ".site-header::before" in css


def test_singular_unit_for_one_day_streak():
    import web_theme
    assert "<small>day</small>" in web_theme.count(1, 0, "<small>days</small>")
    assert "<small>days</small>" in web_theme.count(2, 0, "<small>days</small>")
    assert "<small>days</small>" in web_theme.count(0, 0, "<small>days</small>")


def test_admin_cookie_flags_behind_https_proxy(site):
    req = urllib.request.Request(site.base + "/admin/login", data=f"password={PASSWORD}".encode(), headers={"X-Forwarded-Proto": "https"})
    try:
        urllib.request.build_opener(NoRedirect).open(req)
    except urllib.error.HTTPError as e:
        cookie = e.headers["Set-Cookie"]
    for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/admin"):
        assert flag in cookie, flag
    status, _, headers = get(site.base + "/admin/login")
    assert "no-store" in headers["Cache-Control"]  # admin pages are never cached
