"""
web.py - the WINTER ARC website: dashboard, calendar, leaderboard, streaks, statistics, participants,
profiles, settings, About the Creator and a read-only admin control room.

* Runs inside the same process as the bot and reads the SAME SQLite database file.
* Pure standard library. Pages are rendered on the server; a small static JavaScript file adds interactions.
* Every request opens its own connection with PRAGMA query_only=ON, so the website can never change records
  (the only write is one audit line when an admin downloads the export).
* Public pages show names and verified numbers only: no notes, Telegram IDs or review comments.
"""
import gzip
import hmac
import contextlib
import hashlib
import logging
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import dashboard
import database as db
import dbconn
import export
import leaderboard
import participants as ppl
import reports
import scoring
import web_charts as charts
from utils import fmt_hours, fmt_minutes
from web_doctor import STATES as DOCTOR_STATES, doctor
from web_theme import (STATIC_DIR, STATIC_FILES, count, empty_state, human_range, initials, layout, meter, page_head,
                       stat_item, stats_row, status_dot)
from web_theme_base import q

log = logging.getLogger("winter_arc.web")
CACHE_SECONDS = 5          # statistics are recalculated at most every 5 s however many visitors there are
SESSION_HOURS = 8          # admin login lasts 8 hours
STATUS_NAME = {"GREEN": "Full green", "HALF": "Partial", "RED": "Failed", "PENDING": "Pending", "HEALTH": "Health day", "EMPTY": "No record"}
REPORT_NAME = {"approved": "Approved", "pending": "Awaiting approval", "correction_requested": "Correction requested", "rejected": "Rejected", "draft": "Draft"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_cache, _cache_lock = {}, threading.Lock()
_attempts, _attempts_lock = {}, threading.Lock()
_static_cache = {}


# ------------------------------------------------------------------ data access
def open_ro(db_path):
    if db.is_remote():  # Turso: the SAME shared connection the bot uses, wrapped so nothing can write through it
        return db.connect_readonly(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class Ctx:
    """Everything one page needs: a read-only connection, the chosen session, cached statistics."""

    def __init__(self, cfg, query):
        self.cfg, self.query = cfg, query
        self.conn = open_ro(cfg.db_path)
        self.sessions = db.list_sessions(self.conn)
        self.default = db.get_active_session(self.conn) or (self.sessions[0] if self.sessions else None)
        self.session = self.default
        sid = (query.get("s") or [""])[0]
        if sid.isdigit():
            self.session = db.get_session(self.conn, int(sid)) or self.default
        self.today = config.today()

    def url(self, path, **params):
        if self.session is not None and self.default is not None and self.session["id"] != self.default["id"]:
            params.setdefault("s", self.session["id"])
        return path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")

    @property
    def summaries(self):
        key = (str(self.cfg.db_path), self.session["id"], self.today)
        now = time.monotonic()
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        lock = getattr(self.conn, "tx_lock", None)  # shared connection: one consistent snapshot, no writer in between
        with (lock if lock is not None else contextlib.nullcontext()):
            data = reports.build_summaries(self.conn, self.session, self.today, only_active=False)
        with _cache_lock:
            _cache[key] = (now, data)
            if len(_cache) > 50:
                _cache.clear()
        return data

    @property
    def active(self):
        return [s for s in self.summaries if s.active]

    @property
    def targets_today(self):
        rows = self.conn.execute("SELECT participant_id, planned_minutes FROM daily_targets WHERE session_id=? AND target_date=?",
                                 (self.session["id"], self.today.isoformat())).fetchall()
        return {r["participant_id"]: r["planned_minutes"] for r in rows}

    def page(self, title, body, active):
        return layout(title, body, active, self.session, self.url, self.sessions)

    def close(self):
        self.conn.close()


# ------------------------------------------------------------------ shared pieces
def day_label(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def link(ctx, s):
    return f'<a href="{q(ctx.url("/participant/" + str(s.participant_id)))}">{q(s.name)}</a>'


def legend():
    items = [("GREEN", "Full green"), ("HALF", "Partial"), ("RED", "Failed"), ("PENDING", "Pending report"), ("HEALTH", "Health day"), ("EMPTY", "No record")]
    return '<div class="legend" aria-label="Legend">' + "".join(f'<span>{status_dot(k)}{t}</span>' for k, t in items) + "</div>"


def day_button(rec, with_data=True):
    st = rec.status
    short = fmt_minutes(rec.minutes).replace(" ", "") if rec.minutes else {"PENDING": "pending", "RED": "failed", "HEALTH": "health"}.get(st, "")
    status = STATUS_NAME[st] + (" (set by admin)" if rec.source == "override" else "")
    study = fmt_minutes(rec.minutes) if rec.minutes else ""
    points = (f"{rec.points.gross}  ({rec.points.study_points} hours + {rec.points.exercise_points} exercise + {rec.points.subject_points} subjects)" if rec.points else "")
    penalty = f"−{rec.penalty}" if rec.penalty else ""
    label = day_label(rec.day)
    aria = f"{label}: {status}" + (f", {study}" if study else "")
    return (f'<button type="button" class="day {st}{" fut" if rec.future else ""}" data-label="{q(label)}" data-status="{q(status)}" data-study="{q(study)}" '
            f'data-report="{q(REPORT_NAME.get(rec.report_status, ""))}" data-points="{q(points)}" data-penalty="{q(penalty)}" aria-pressed="false" aria-label="{q(aria)}">'
            f'{rec.day.day}<small>{q(short)}</small></button>')


def month_calendars(s):
    out = []
    for y, m in sorted({(d.year, d.month) for d in s.days}):
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        cells = [f'<div class="wd" aria-hidden="true">{n}</div>' for n in ("S", "M", "T", "W", "T", "F", "S")]
        cells += ['<div class="day out" aria-hidden="true"></div>'] * ((first.weekday() + 1) % 7)
        d = first
        while d < nxt:
            cells.append(day_button(s.days[d]) if d in s.days else '<div class="day out" aria-hidden="true"></div>')
            d += timedelta(days=1)
        out.append(f'<div class="cal-month reveal"><h3>{first:%B %Y}</h3><div class="cal">{"".join(cells)}</div></div>')
    return "".join(out)


def calendar_block(s):
    return (f'<div class="cal-layout">{"<div>" + legend() + month_calendars(s) + "</div>"}'
            '<aside class="card panel reveal" id="day-panel" aria-live="polite"><h3>Select a day</h3><p class="muted small" style="margin-top:8px">'
            'Tap or click any date to see its status, verified study time and points.</p></aside></div>')


def heatmap(ctx, ordered):
    days = sorted(ordered[0].days) if ordered else []
    head = '<tr><th class="nm"><span class="sr-only">Participant</span></th>' + "".join(f"<th>{d.day}</th>" for d in days) + "</tr>"
    body = ""
    for s in ordered:
        cells = "".join(f'<td class="c {s.days[d].status}{" fut" if s.days[d].future else ""}" title="{q(s.name)} · {d.isoformat()}: {q(STATUS_NAME[s.days[d].status])}"></td>' for d in days)
        body += f'<tr><td class="nm">{link(ctx, s)}</td>{cells}</tr>'
    return f'<div class="card heat-wrap reveal"><table class="heat">{head}{body}</table></div>'


def chart_card(title, sub, svg, extra=""):
    return f'<div class="card reveal"><div class="eyebrow">{q(title)}</div><p class="muted small" style="margin:4px 0 14px">{q(sub)}</p>{svg}{extra}</div>'


def today_progress(ctx, s):
    rec = s.days.get(ctx.today)
    if rec is None:
        return '<span class="muted small">Not running today</span>'
    planned = ctx.targets_today.get(s.participant_id)
    if rec.report_status in ("pending", "correction_requested"):
        return f'<span class="small">{status_dot("PENDING")} Report pending</span>'
    if planned:
        pct = min(100, rec.minutes / planned * 100)
        return f'<small>{fmt_minutes(rec.minutes)} of {fmt_minutes(planned)}</small>{meter(pct)}'
    return '<span class="muted small">No target set</span>'


def doctor_state_for(s, today):
    rec = s.days.get(today)
    if rec is None:
        return "welcome"
    if rec.report_status in ("pending", "correction_requested"):
        return "pending"
    if rec.status == "GREEN":
        return "completed"
    if rec.status == "HEALTH":
        return "rest"
    if rec.report_status == "approved":
        return "approved"
    return "progress"


# ------------------------------------------------------------------ public pages
def page_home(ctx):
    sess, sm = ctx.session, ctx.summaries
    data = dashboard.dashboard_data(sess, sm, ctx.today)
    ranked = leaderboard.rank(sm, "net")
    total, dayno = data["total_days"], data["day_no"]
    pct = min(100, dayno / total * 100) if total else 0
    start, end = reports.session_range(sess)
    hero = (f'<section class="hero"><div class="reveal"><div class="eyebrow">Session {int(sess["session_number"]):02d} · {q(sess["group_name"])}</div>'
            f'<h1 class="display" style="margin-top:12px">Winter Arc</h1><div class="sub"><span>{q(human_range(sess))}</span></div>'
            f'<div class="progress"><div class="row"><span>Challenge progress</span><span>Day {dayno} of {total}</span></div>{meter(pct)}</div>'
            f'<div class="btn-row"><a class="btn" href="{q(ctx.url("/leaderboard"))}">View leaderboard</a><a class="btn ghost" href="{q(ctx.url("/calendar"))}">Open calendar</a></div></div>'
            f'{doctor("welcome", 132)}</section>')
    stats = stats_row([
        stat_item("Registered participants", count(data["active_participants"])),
        stat_item("Verified study", count(data["verified_minutes"] / 60, 1, "<small>h</small>"), "approved reports only"),
        stat_item("Net points", count(data["net_points"]), f"gross {data['gross_points']} − penalties {data['penalty_total']}"),
        stat_item("Leading streak", count(data["longest_current_streak"], 0, "<small>days</small>"), data["streak_holder"] if data["streak_holder"] != "-" else "no active streak yet"),
        stat_item("Challenge progress", count(pct, 0, "<small>%</small>"), f"day {dayno} of {total}")])
    counts = {}
    for s in ctx.active:
        if ctx.today in s.days:
            st = s.days[ctx.today].status
            counts[st] = counts.get(st, 0) + 1
    state = "welcome" if ctx.today < start else "completed" if ctx.today > end else "progress"
    chips = "".join(f'<span class="chip">{status_dot(k)}{STATUS_NAME[k]} <b class="num">{v}</b></span>' for k, v in sorted(counts.items())) or '<span class="muted">The session is not running today.</span>'
    today_card = (f'<section class="section"><div class="card reveal" style="display:flex;gap:20px;align-items:center;justify-content:space-between;flex-wrap:wrap">'
                  f'<div><div class="eyebrow">Today · {day_label(ctx.today)}</div><h2 class="h2" style="margin:6px 0 14px">How the team is doing</h2><div class="chips">{chips}</div></div>'
                  f'{doctor(state, 84, decorative=True)}</div></section>')
    if ranked:
        rows = "".join(f'<tr class="{"top" if r <= 3 else ""}"><td class="rank"><span class="medal m{r if r <= 3 else 0}">{r}</span></td><td class="name">{link(ctx, s)}</td>'
                       f'<td class="r num" data-l="Net points"><b>{s.net_points}</b> <span class="muted small">pts</span></td><td class="r num" data-l="Hours">{fmt_hours(s.verified_minutes)}</td></tr>' for r, s in ranked[:3])
        board = (f'<section class="section"><div class="head reveal"><h2 class="h2">Leading the board</h2><a class="link-arrow" href="{q(ctx.url("/leaderboard"))}">Full leaderboard</a></div>'
                 f'<div class="card pad-0 reveal"><table class="board"><tbody>{rows}</tbody></table></div></section>')
    else:
        board = f'<section class="section">{empty_state("No participants yet", "Registered participants will appear here as soon as they join the challenge in Telegram.")}</section>'
    explore = "".join(f'<a class="card interactive" href="{q(ctx.url(p))}"><div class="eyebrow">{e}</div><h3 class="h2" style="margin-top:6px">{t}</h3><p class="muted small" style="margin-top:6px">{d}</p></a>'
                      for p, e, t, d in (("/calendar", "Calendar", "Every day at a glance", "Monthly attendance for each participant."),
                                         ("/stats", "Statistics", "The numbers behind the challenge", "Study hours, points and consistency over time."),
                                         ("/participants", "Participants", "Everyone taking part", "Profiles with streaks and verified points.")))
    return ctx.page("Home", hero + stats + today_card + board + f'<section class="section"><div class="grid g3 stagger">{explore}</div></section>', "home")


def page_leaderboard(ctx, kind, page):
    kind = kind if kind in leaderboard.BOARDS else "net"
    ranked = leaderboard.rank(ctx.summaries, kind)
    size = 25
    pages = max(1, -(-len(ranked) // size))
    page = max(0, min(page, pages - 1))
    tabs = "".join(f'<a href="{q(ctx.url("/leaderboard", kind=k))}"{" aria-current=true" if k == kind else ""}>{q(v[0])}</a>' for k, v in leaderboard.BOARDS.items())
    head = page_head("Leaderboard", leaderboard.BOARDS[kind][1], "Ranked by admin-verified results only.")
    if not ranked:
        return ctx.page("Leaderboard", head + empty_state("The board is empty", "As soon as participants register and have a report approved, they will be ranked here."), "leaderboard")
    main = {"net": lambda s: str(s.net_points), "hours": lambda s: fmt_hours(s.verified_minutes), "streak": lambda s: f"{s.longest_streak} days",
            "days": lambda s: f"{s.success_days} days", "avg": lambda s: f"{fmt_hours(s.avg_minutes)}/day"}[kind]
    rows = ""
    for r, s in ranked[page * size:(page + 1) * size]:
        net_cell = "" if kind == "net" else f'<td class="r num" data-l="Net points">{s.net_points}</td>'
        rows += (f'<tr class="{"top" if r <= 3 else ""}"><td class="rank"><span class="medal m{r if r <= 3 else 0}">{r}</span></td><td class="name">{link(ctx, s)}</td>'
                 f'<td class="r num" data-l="{q(leaderboard.BOARDS[kind][1])}"><b>{q(main(s))}</b></td><td class="r num" data-l="Hours">{fmt_hours(s.verified_minutes)}</td>'
                 f'{net_cell}<td class="r num" data-l="Streak">{s.current_streak}</td><td class="today-cell" data-l="Today\'s target">{today_progress(ctx, s)}</td></tr>')
    table = ('<div class="card pad-0 reveal"><table class="board"><thead><tr><th>#</th><th>Participant</th><th class="r">' + q(leaderboard.BOARDS[kind][1]) +
             '</th><th class="r">Hours</th>' + ('' if kind == 'net' else '<th class="r">Net</th>') + '<th class="r">Streak</th><th>Today\'s target</th></tr></thead><tbody>' + rows + '</tbody></table></div>')
    pager = ""
    if pages > 1:
        pager = '<div class="seg" style="margin-top:20px">' + "".join(f'<a href="{q(ctx.url("/leaderboard", kind=kind, page=i))}"{" aria-current=true" if i == page else ""}>{i + 1}</a>' for i in range(pages)) + "</div>"
    note = '<p class="muted small" style="margin-top:16px">Ties are broken by net points, then hours, then longest streak, then name.</p>'
    return ctx.page("Leaderboard", f'{head}<div class="seg reveal" style="margin-bottom:20px">{tabs}</div>{table}{pager}{note}', "leaderboard")


def streak_series(act, days):
    runs = {s.participant_id: 0 for s in act}
    best = []
    for d in days:
        top = 0
        for s in act:
            runs[s.participant_id] = runs[s.participant_id] + 1 if s.days[d].status == scoring.GREEN else 0
            top = max(top, runs[s.participant_id])
        best.append(top)
    return best


def elapsed_days(ctx):
    start, end = reports.session_range(ctx.session)
    last = min(end, ctx.today)
    return list(scoring.daterange(start, last)) if last >= start else []


def page_streaks(ctx):
    act = sorted(ctx.active, key=lambda s: (-s.current_streak, -s.longest_streak, -s.net_points, s.name.casefold(), s.participant_id))
    head = page_head("Streaks", "Consistency, day after day", "A streak is a run of consecutive full-green, approved days.", "progress")
    if not act:
        return ctx.page("Streaks", head + empty_state("No streaks yet", "Streaks begin once participants have approved full-green days."), "streaks")
    days = elapsed_days(ctx)
    total = max(1, len(days))
    lead = act[0]
    hero = stats_row([stat_item("Leading streak", count(lead.current_streak, 0, "<small>days</small>"), lead.name if lead.current_streak else "no active streak yet"),
                      stat_item("Longest streak so far", count(max(s.longest_streak for s in act), 0, "<small>days</small>")),
                      stat_item("Participants on a streak", count(sum(1 for s in act if s.current_streak > 0)))])
    rows = "".join(f'<tr><td class="rank"><span class="medal m{i if i <= 3 else 0}">{i}</span></td><td class="name">{link(ctx, s)}</td>'
                   f'<td class="r num" data-l="Current"><b>{s.current_streak}</b></td><td data-l="Progress" style="min-width:140px">{meter(s.current_streak / total * 100)}</td>'
                   f'<td class="r num" data-l="Longest">{s.longest_streak}</td><td class="r num" data-l="Full-green days">{s.success_days}</td></tr>' for i, s in enumerate(act, 1))
    table = ('<div class="card pad-0 reveal" style="margin-top:28px"><table class="board"><thead><tr><th>#</th><th>Participant</th><th class="r">Current</th><th></th>'
             '<th class="r">Longest</th><th class="r">Green days</th></tr></thead><tbody>' + rows + '</tbody></table></div>')
    best = streak_series(act, days)
    hist = chart_card("Streak history", "The longest active streak on each day.", charts.line_chart(
        best, [str(d.day) for d in days], [f"{day_label(d)}: best streak {v} day{'s' if v != 1 else ''}" for d, v in zip(days, best)],
        "Line chart of the longest active streak per day", fmt=lambda v: f"{v:.0f}")) if days else ""
    return ctx.page("Streaks", head + hero + table + f'<section class="section">{hist}</section>', "streaks")


def page_stats(ctx):
    sess, act = ctx.session, ctx.active
    data = dashboard.dashboard_data(sess, ctx.summaries, ctx.today)
    head = page_head("Statistics", "The challenge in numbers", "Calculated live from admin-approved records.", "progress")
    days = elapsed_days(ctx)
    if not act or not days:
        return ctx.page("Statistics", head + empty_state("Nothing to chart yet", "Charts appear once the challenge has started and participants have approved reports.", "empty"), "stats")
    total_days = data["total_days"]
    pct = min(100, data["day_no"] / total_days * 100) if total_days else 0
    cells = len(act) * len(days)
    status_counts = {k: 0 for k in STATUS_NAME}
    for s in act:
        for d in days:
            status_counts[s.days[d].status] += 1
    consistency = status_counts["GREEN"] / cells * 100 if cells else 0
    completion = data["approved_reports"] / cells * 100 if cells else 0
    top = stats_row([
        stat_item("Participants", count(data["active_participants"])), stat_item("Verified study", count(data["verified_minutes"] / 60, 1, "<small>h</small>")),
        stat_item("Net points", count(data["net_points"]), f"gross {data['gross_points']} − penalties {data['penalty_total']}"),
        stat_item("Full-green days", count(data["success_days"]), f"{data['partial_days']} partial"), stat_item("Pending reports", count(data["pending_reports"]))])
    daily = [sum(s.days[d].minutes for s in act) / 60 for d in days]
    c_daily = chart_card("Daily study hours", "Total verified hours across all participants.", charts.bar_chart(
        daily, [str(d.day) for d in days], [f"{day_label(d)}: {v:.1f} h" for d, v in zip(days, daily)], "Bar chart of verified study hours per day", fmt=lambda v: f"{v:g}"))
    start = days[0]
    weeks = {}
    for d, v in zip(days, daily):
        w = (d - start).days // 7
        tot, n = weeks.get(w, (0, 0))
        weeks[w] = (tot + v, n + 1)
    wk = sorted(weeks)
    wvals = [weeks[w][0] for w in wk]
    c_week = chart_card("Weekly performance", "Verified hours per week of the challenge.", charts.bar_chart(
        wvals, [f"W{w + 1}" for w in wk], [f"Week {w + 1}: {weeks[w][0]:.1f} h · {weeks[w][0] / max(1, weeks[w][1] * len(act)):.1f} h per participant per day" for w in wk],
        "Bar chart of verified study hours per week", fmt=lambda v: f"{v:g}", every=1))
    cum, running = [], 0
    for d in days:
        running += sum((s.days[d].points.gross if s.days[d].points else 0) - s.days[d].penalty for s in act)
        cum.append(running)
    c_points = chart_card("Verified points over time", "Cumulative net points of all participants.", charts.line_chart(
        cum, [str(d.day) for d in days], [f"{day_label(d)}: {v} net points" for d, v in zip(days, cum)], "Line chart of cumulative net points", fmt=lambda v: f"{v:.0f}"))
    best = streak_series(act, days)
    c_streak = chart_card("Streak history", "Longest active streak on each day.", charts.line_chart(
        best, [str(d.day) for d in days], [f"{day_label(d)}: best streak {v}" for d, v in zip(days, best)], "Line chart of the longest active streak per day", fmt=lambda v: f"{v:.0f}"))
    colors = ["GREEN", "HALF", "RED", "PENDING", "HEALTH", "EMPTY"]
    stack = "".join(f'<i class="dot {k}" style="--f:{status_counts[k]};border-radius:0;box-shadow:none;height:100%;width:auto" title="{STATUS_NAME[k]}: {status_counts[k]}"></i>' for k in colors if status_counts[k])
    legend_ = '<div class="legend" style="margin:14px 0 0">' + "".join(f'<span>{status_dot(k)}{STATUS_NAME[k]} <b class="num">{status_counts[k]}</b></span>' for k in colors) + "</div>"
    c_att = (f'<div class="card reveal"><div class="eyebrow">Attendance consistency</div><div class="bignum">{count(consistency, 0, "<small>%</small>")}</div>'
             f'<div class="stack" role="img" aria-label="Share of participant-days by status">{stack}</div>{legend_}'
             f'<p class="muted small" style="margin-top:12px">Share of participant-days that were full green. Report completion: {completion:.0f}% of participant-days have an approved report.</p></div>')
    prog = (f'<div class="card reveal"><div class="eyebrow">Overall challenge progress</div><div class="bignum">{count(pct, 0, "<small>%</small>")}</div>'
            f'{meter(pct)}<p class="muted small" style="margin-top:10px">Day {data["day_no"]} of {total_days} · {q(human_range(sess))}</p></div>')
    totals = stats_row([stat_item("Physics questions", count(sum(s.physics_total for s in act))), stat_item("Chemistry questions", count(sum(s.chemistry_total for s in act))),
                        stat_item("Biology questions", count(sum(s.biology_total for s in act))), stat_item("Exercise sessions", count(sum(s.exercise_total for s in act)))])
    rows = "".join(f'<tr><td class="rank"><span class="medal m{i if i <= 3 else 0}">{i}</span></td><td class="name">{link(ctx, s)}</td><td class="r num" data-l="Net points"><b>{s.net_points}</b> <span class="muted small">pts</span></td><td class="r num" data-l="Hours">{fmt_hours(s.verified_minutes)}</td></tr>'
                   for i, s in enumerate(data["top5"], 1))
    top5 = f'<div class="card pad-0 reveal"><table class="board"><tbody>{rows}</tbody></table></div>'
    body = (head + top + f'<section class="section"><div class="grid g2">{prog}{c_att}</div></section><section class="section"><div class="grid g2">{c_daily}{c_week}</div></section>'
            f'<section class="section"><div class="grid g2">{c_points}{c_streak}</div></section><section class="section"><div class="head"><h2 class="h2">Questions &amp; exercise</h2></div>{totals}</section>'
            f'<section class="section"><div class="head"><h2 class="h2">Top five</h2></div>{top5}</section>')
    return ctx.page("Statistics", body, "stats")


def page_calendar(ctx, pid):
    ordered = [s for _, s in leaderboard.rank(ctx.summaries, "net")]
    head = page_head("Calendar", "Study calendar", "Every day, every participant. Select a date for details.", "progress")
    if not ordered:
        return ctx.page("Calendar", head + empty_state("No participants yet", "The calendar fills in once participants join and submit reports."), "calendar")
    opts = "".join(f'<option value="{s.participant_id}"{" selected" if s.participant_id == pid else ""}>{q(s.name)}</option>' for s in ordered)
    sess = f'<input type="hidden" name="s" value="{ctx.session["id"]}">' if ctx.session["id"] != ctx.default["id"] else ""
    form = (f'<form class="form-row reveal" method="get" action="/calendar" data-noload>{sess}<label class="sr-only" for="pick">Participant</label>'
            f'<select id="pick" name="p" data-autosubmit><option value="">Whole team</option>{opts}</select><button class="btn ghost" type="submit">Show</button></form>')
    chosen = next((s for s in ctx.summaries if s.participant_id == pid), None)
    body = head + form
    if chosen:
        body += (f'<section class="section"><div class="head"><h2 class="h2">{link(ctx, chosen)}</h2><span class="muted num">Net {chosen.net_points} · streak {chosen.current_streak}</span></div>{calendar_block(chosen)}</section>')
    body += f'<section class="section"><div class="head"><h2 class="h2">Team overview</h2></div>{legend()}{heatmap(ctx, ordered)}</section>'
    return ctx.page("Calendar", body, "calendar")


def page_participants(ctx):
    people = sorted(ctx.active, key=lambda s: (s.name.casefold(), s.participant_id))
    head = page_head("Participants", "Everyone taking part", f"{len(people)} registered" if people else "")
    if not people:
        return ctx.page("Participants", head + empty_state("No one has joined yet", "Participants appear here after registering with the Telegram bot."), "participants")
    cards = ""
    for s in people:
        rec = s.days.get(ctx.today)
        st = rec.status if rec else "EMPTY"
        cards += (f'<a class="card interactive" href="{q(ctx.url("/participant/" + str(s.participant_id)))}"><div class="person"><div class="initials">{q(initials(s.name))}</div>'
                  f'<div class="meta"><b>{q(s.name)}</b><span class="muted small">{"School-going" if s.school_type == "school" else "Dummy-school"}</span></div></div>'
                  f'<div class="chips" style="margin-top:16px"><span class="chip">Net <b class="num">{s.net_points}</b></span><span class="chip">Streak <b class="num">{s.current_streak}</b></span>'
                  f'<span class="chip" title="Today">{status_dot(st)}{STATUS_NAME[st]}</span></div></a>')
    return ctx.page("Participants", f'{head}<div class="grid g3 stagger">{cards}</div>', "participants")


def page_participant(ctx, pid):
    s = next((x for x in ctx.summaries if x.participant_id == pid), None)
    if s is None:
        return None
    days = elapsed_days(ctx)
    state = doctor_state_for(s, ctx.today)
    hero = (f'<div class="page-head reveal"><div class="person"><div class="initials" style="width:64px;height:64px;font-size:1.3rem">{q(initials(s.name))}</div><div class="meta">'
            f'<div class="eyebrow">{"School-going" if s.school_type == "school" else "Dummy-school"}{"" if s.active else " · inactive"}</div><h1 class="h1" style="margin-top:4px">{q(s.name)}</h1></div></div>{doctor(state, 96)}</div>')
    stats = stats_row([stat_item("Net points", count(s.net_points), f"gross {s.gross_points} − penalties {s.penalty_total}"),
                       stat_item("Verified study", count(s.verified_minutes / 60, 1, "<small>h</small>"), f"avg {fmt_hours(s.avg_minutes)} per day"),
                       stat_item("Current streak", count(s.current_streak, 0, "<small>days</small>"), f"longest {s.longest_streak}"),
                       stat_item("Full-green days", count(s.success_days), f"{s.partial_days} partial · {s.unsuccessful_days} failed"),
                       stat_item("Reports", count(s.approved_reports), f"{s.pending_reports} pending · {s.missing_reports} missing")])
    extra = ""
    if days:
        hrs = [s.days[d].minutes / 60 for d in days]
        cum, run = [], 0
        for d in days:
            run += (s.days[d].points.gross if s.days[d].points else 0) - s.days[d].penalty
            cum.append(run)
        extra = (f'<section class="section"><div class="grid g2">'
                 + chart_card("Daily study hours", "Verified hours per day.", charts.bar_chart(hrs, [str(d.day) for d in days], [f"{day_label(d)}: {v:.1f} h" for d, v in zip(days, hrs)],
                                                                                              f"Bar chart of {s.name}'s verified study hours per day", fmt=lambda v: f"{v:g}"))
                 + chart_card("Net points over time", "Cumulative verified points.", charts.line_chart(cum, [str(d.day) for d in days], [f"{day_label(d)}: {v} net points" for d, v in zip(days, cum)],
                                                                                                         f"Line chart of {s.name}'s cumulative net points", fmt=lambda v: f"{v:.0f}", vmin=min(0, min(cum))))
                 + "</div></section>")
    subj = (f'<section class="section"><div class="head"><h2 class="h2">Questions &amp; exercise</h2></div>' + stats_row([
        stat_item("Physics", count(s.physics_total), f"{s.physics_days} days at 15+"), stat_item("Chemistry", count(s.chemistry_total), f"{s.chemistry_days} days at 15+"),
        stat_item("Biology", count(s.biology_total), f"{s.biology_days} days at 15+"), stat_item("Exercise", count(s.exercise_total), f"on {s.exercise_days} days")]) + "</section>")
    rows = ""
    for d, r in sorted(s.days.items(), reverse=True):
        if r.points is not None or r.penalty:
            p = r.points
            rows += (f'<tr><td>{day_label(d)}</td><td>{status_dot(r.status)} {STATUS_NAME[r.status]}</td><td class="r num">{fmt_minutes(p.raw_minutes) if p else "–"}</td>'
                     f'<td class="r num">{p.study_points if p else "–"}</td><td class="r num">{p.exercise_points if p else "–"}</td><td class="r num">{p.subject_points if p else "–"}</td>'
                     f'<td class="r num"><b>{p.gross if p else 0}</b></td><td class="r num">{"−" + str(r.penalty) if r.penalty else "0"}</td></tr>')
    table = (('<div class="card pad-0 scroll-x reveal"><table class="tbl"><thead><tr><th>Date</th><th>Status</th><th class="r">Study</th><th class="r">Hours</th><th class="r">Exercise</th><th class="r">Subjects</th>'
              '<th class="r">Gross</th><th class="r">Penalty</th></tr></thead><tbody>' + rows + "</tbody></table></div>") if rows else empty_state("No approved days yet", "Points appear here once an admin approves a report."))
    body = (hero + stats + extra + f'<section class="section"><div class="head"><h2 class="h2">Calendar</h2></div>{calendar_block(s)}</section>' + subj
            + f'<section class="section"><div class="head"><h2 class="h2">Daily points</h2></div>{table}</section>')
    return ctx.page(s.name, body, "participants")


def page_about(ctx):
    c = config.CREATOR
    pillars = "".join(f'<div>{a}<small>{b}</small></div>' for a, b in (("Discipline", "show up on purpose"), ("Consistency", "day after day"), ("Focused study", "deep, undistracted work"), ("Personal growth", "a little better each day")))
    body = (f'<section class="about-hero"><div class="eyebrow reveal">About the Creator</div><div class="reveal" style="--i:1">{doctor("welcome", 104)}</div>'
            f'<h1 class="name reveal" style="--i:2">{q(c["name"])}</h1><a class="handle reveal" style="--i:3" href="{q(config.creator_telegram_url())}" target="_blank" rel="noopener" aria-label="Open @{q(c["telegram_username"])} on Telegram">@{q(c["telegram_username"])}</a></section>'
            f'<div class="about-body stagger"><p>{q(c["project"])} is a study challenge built on a simple belief: lasting progress comes from showing up every day, not from rare bursts of effort.</p>'
            '<p>Each day, participants set a target, study with focus and report honestly. An admin reviews every report before it counts, so every number on this site is earned rather than claimed.</p>'
            f'<div class="pillars">{pillars}</div>'
            f'<p class="muted" style="font-size:1rem">Questions, feedback or ideas? Message <a class="accent" href="{q(config.creator_telegram_url())}" target="_blank" rel="noopener">@{q(c["telegram_username"])}</a> on Telegram.</p></div>')
    return ctx.page("About the Creator", body, "about")


def page_settings(ctx):
    def seg(key, options):
        return f'<div class="seg" data-setting="{key}">' + "".join(f'<button type="button" data-value="{v}" aria-pressed="false">{t}</button>' for v, t in options) + "</div>"
    gallery = "".join(f'<figure>{doctor(k, 84)}<figcaption>{k}</figcaption></figure>' for k in DOCTOR_STATES)
    body = (page_head("Settings", "Make it yours", "Preferences are saved in this browser only.")
            + '<div class="card reveal"><div class="setting"><div><b>Theme</b><div class="muted">Light is the default.</div></div>' + seg("theme", [("light", "Light"), ("dark", "Dark"), ("system", "System")]) + "</div>"
            '<div class="setting"><div><b>Opening scene</b><div class="muted">The Study Sanctuary desk-lamp introduction.</div></div>' + seg("intro", [("first", "First visit"), ("always", "Every visit"), ("never", "Never")]) + "</div>"
            '<div class="setting"><div><b>Animations</b><div class="muted">Reduce motion across the site.</div></div>' + seg("motion", [("system", "Follow device"), ("reduce", "Reduce")]) + "</div>"
            '<div class="setting"><div><b>Replay the introduction</b><div class="muted">Watch the desk-lamp scene again.</div></div><button type="button" class="btn ghost" id="replay-intro">Replay intro</button></div></div>'
            f'<section class="section"><div class="head"><h2 class="h2">Meet Dr. Arc</h2></div><p class="muted" style="margin-bottom:16px">The study assistant shows where things stand. It never gives medical advice.</p><div class="gallery reveal">{gallery}</div></section>')
    return ctx.page("Settings", body, "settings")


def error_page(ctx, status, title, text):
    body = (f'<div class="empty">{doctor("error", 110)}<h1 class="h1">{q(title)}</h1><p>{q(text)}</p><div class="btn-row" style="justify-content:center"><a class="btn" href="/">Back to home</a></div></div>')
    if ctx is not None and ctx.session is not None:
        return ctx.page(title, body, "home")
    return layout(title, body, "home")


# ------------------------------------------------------------------ admin (read-only control room)
def make_token(secret: str) -> str:
    exp = str(int(time.time()) + SESSION_HOURS * 3600)
    return exp + "." + hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()


def token_ok(secret: str, token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        good = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def page_login(ctx, message=""):
    enabled = bool(ctx.cfg.web_admin_password)
    note = ""
    state = "welcome"
    if not enabled:
        note, state = '<div class="notice">The admin page is switched off. Set WEB_ADMIN_PASSWORD in the hosting settings to turn it on.</div>', "rest"
    elif message:
        note, state = f'<div class="notice bad" role="alert">{q(message)}</div>', "error"
    form = ('<form class="form-row" method="post" action="/admin/login"><label class="sr-only" for="pw">Admin password</label>'
            '<input id="pw" type="password" name="password" placeholder="Admin password" autocomplete="current-password" required>'
            '<button class="btn" type="submit">Log in</button></form>') if enabled else ""
    body = f'<div class="login reveal">{doctor(state, 104)}<div class="eyebrow">Admin</div><h1 class="h1" style="margin:6px 0 20px">Welcome back</h1>{note}{form}</div>'
    return ctx.page("Admin login", body, "admin")


def db_members(conn, session_id):
    return conn.execute("SELECT p.*, m.is_active AS member_active FROM session_members m JOIN participants p ON p.id=m.participant_id "
                        "WHERE m.session_id=? ORDER BY lower(p.display_name), p.id", (session_id,)).fetchall()


def page_admin(ctx):
    c, sid = ctx.conn, ctx.session["id"]
    sm = ctx.summaries
    warns = "".join(f'<div class="notice">{q(w)}</div>' for w in config.deploy_warnings(ctx.cfg))
    pend = reports.pending_reports(c, sid)
    unreachable = c.execute("SELECT DISTINCT p.display_name FROM reminders_sent r JOIN participants p ON p.id=r.participant_id WHERE r.status='blocked' ORDER BY 1").fetchall()
    top = stats_row([stat_item("Pending approval", count(len(pend)), "approve in Telegram with /admin"), stat_item("Participants", count(sum(1 for s in sm if s.active))),
                     stat_item("Inactive or removed", count(sum(1 for s in sm if not s.active))), stat_item("Unreachable in Telegram", count(len(unreachable)), "have not pressed Start")])
    subnav = '<nav class="subnav" aria-label="Admin sections">' + "".join(f'<a href="#{a}">{t}</a>' for a, t in (("pending", "Pending"), ("penalties", "Penalties"), ("people", "Participants"), ("activity", "Activity"), ("storage", "Storage"))) + "</nav>"
    actions = (f'<div class="btn-row" style="margin:0 0 8px"><a class="btn" href="/admin/export.xlsx?s={sid}" data-confirm="The export contains participant names and all verified records. Download it?" '
               'data-confirm-title="Download the XLSX export?" data-confirm-label="Download">Download XLSX export</a>'
               '<form method="post" action="/admin/logout" data-noload><button class="btn ghost" type="submit" data-confirm="You will need the password to come back." data-confirm-title="Log out of admin?" data-confirm-label="Log out">Log out</button></form></div>')
    if pend:
        prow = "".join(f'<tr><td>{q(r["display_name"])}</td><td>{r["report_date"]}</td><td class="r num">{fmt_minutes(r["independent_minutes"] + r["school_minutes"])}</td><td>{q(r["submitted_at"][:16].replace("T", " "))}</td></tr>' for r in pend[:30])
        pending_html = f'<div class="card pad-0 scroll-x"><table class="tbl"><thead><tr><th>Participant</th><th>Date</th><th class="r">Study</th><th>Submitted</th></tr></thead><tbody>{prow}</tbody></table></div><p class="muted small" style="margin-top:10px">Approve, reject or request corrections in Telegram: /admin → Pending reports.</p>'
    else:
        pending_html = empty_state("Nothing is waiting", "Every submitted report has been reviewed.", "approved")
    pen = reports.list_penalties(c, sid)[:25]
    pen_html = ('<div class="card pad-0 scroll-x"><table class="tbl"><thead><tr><th>#</th><th>Participant</th><th>Date</th><th>Type</th><th class="r">Amount</th><th>Status</th><th>Reason</th></tr></thead><tbody>'
                + "".join(f'<tr><td>{p["id"]}</td><td>{q(p["display_name"])}</td><td>{p["penalty_date"]}</td><td>{q(p["kind"])}</td><td class="r num">−{p["amount"]}</td><td>{q(p["status"])}</td><td>{q(p["reason"])}</td></tr>' for p in pen)
                + "</tbody></table></div>") if pen else '<div class="card soft muted">No penalties recorded.</div>'
    mem = ('<div class="card pad-0 scroll-x"><table class="tbl"><thead><tr><th>ID</th><th>Name</th><th>Type</th><th>Telegram</th><th>State</th></tr></thead><tbody>' + "".join(
        f'<tr><td>{m["id"]}</td><td>{q(m["display_name"])}</td><td>{"School" if m["school_type"] == "school" else "Dummy"}</td><td>{"linked" if m["telegram_user_id"] else "admin-managed"}</td>'
        f'<td>{"active" if m["is_active"] and m["member_active"] else "inactive"}</td></tr>' for m in db_members(c, sid)) + "</tbody></table></div>")
    unreach = ('<p class="muted small" style="margin-top:12px">Cannot be messaged yet: ' + ", ".join(q(u["display_name"]) for u in unreachable) + "</p>") if unreachable else ""
    audit = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 25").fetchall()
    act_html = ('<div class="card pad-0 scroll-x"><table class="tbl"><thead><tr><th>When (IST)</th><th>Admin ID</th><th>Action</th><th>Entity</th><th>ID</th></tr></thead><tbody>' + "".join(
        f'<tr><td>{q(a["created_at"][5:16].replace("T", " "))}</td><td>{a["actor_id"]}</td><td>{q(a["action"])}</td><td>{q(a["entity"] or "")}</td><td>{q(a["entity_id"] or "")}</td></tr>' for a in audit) + "</tbody></table></div>")
    if db.is_remote():
        last_ok, last_err = db.get_setting(c, "offsite_backup_last_ok", ""), db.get_setting(c, "offsite_backup_last_error", "")
        where = "the private backup group" if ctx.cfg.backup_chat_id else "the admins' private chats"
        storage = (f'<div class="card"><p>Database <b>{q(db.describe_backend())}</b> · schema v{db.schema_version(c)} · shared by the bot and this website</p>'
                   f'<p class="muted" style="margin-top:6px">Last off-site backup: <b>{q(last_ok.replace("T", " ")[:16]) if last_ok else "none yet"}</b> → {where} '
                   f'(every {ctx.cfg.backup_every_hours} h, copies kept {ctx.cfg.backup_keep} days locally)</p>'
                   + (f'<p class="muted small" style="margin-top:6px">Last backup problem: {q(last_err)}</p>' if last_err else "")
                   + '<p class="muted small" style="margin-top:6px">Restore instructions: BACKUP-RESTORE.md. Send /backup to the bot for a copy right now.</p></div>')
    else:
        size = ctx.cfg.db_path.stat().st_size / 1024 if ctx.cfg.db_path.exists() else 0
        backups = sorted(ctx.cfg.backup_dir.glob("arc-*.db"), reverse=True)[:8] if ctx.cfg.backup_dir.exists() else []
        storage = (f'<div class="card"><p>Database <b>{q(ctx.cfg.db_path.name)}</b> · {size:,.0f} KB · schema v{db.schema_version(c)}</p>'
                   f'<p class="muted" style="margin-top:6px">Latest backups: {", ".join(q(b.name) for b in backups) or "none yet"}</p>'
                   '<p class="muted small" style="margin-top:6px">Backups live next to the database. Send /backup to the bot to receive a copy in Telegram.</p></div>')

    def sec(i, t, inner):
        return f'<section class="section" id="{i}"><div class="head"><h2 class="h2">{t}</h2></div>{inner}</section>'
    body = (page_head("Admin", "Control room", "A read-only view of the live database.", "pending" if pend else "approved") + warns + top + subnav + actions
            + sec("pending", "Pending approval", pending_html) + sec("penalties", "Penalty ledger", pen_html) + sec("people", "Participants", mem + unreach)
            + sec("activity", "Recent admin activity", act_html) + sec("storage", "Storage", storage))
    return ctx.page("Admin", body, "admin")


# ------------------------------------------------------------------ HTTP layer
def read_static(name):
    """Return (bytes, gzipped_bytes, content_type) for an allow-listed static file."""
    if name not in STATIC_FILES:
        return None
    path = STATIC_DIR / name
    mtime = path.stat().st_mtime
    hit = _static_cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]
    raw = path.read_bytes()
    result = (raw, gzip.compress(raw, 6), STATIC_FILES[name])
    _static_cache[name] = (mtime, result)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "WinterArc"
    sys_version = ""
    timeout = 10  # drop slow/stalled connections

    def log_message(self, fmt, *args):  # no query strings / cookies in logs
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body, ctype="text/html; charset=utf-8", headers=(), head_only=False, cache=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", cache or ("no-store" if self.path.startswith("/admin") else "public, max-age=5"))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"

    def _is_admin(self):
        raw = self.headers.get("Cookie", "")
        if not raw or not self.server.cfg.web_admin_password:
            return False
        try:
            jar = SimpleCookie(raw)
        except Exception:  # noqa: BLE001
            return False
        return "wa_admin" in jar and token_ok(self.server.secret, jar["wa_admin"].value)

    def _cookie(self, value, max_age):
        return ("Set-Cookie", f"wa_admin={value}; Path=/admin; HttpOnly; SameSite=Strict; Max-Age={max_age}" + ("; Secure" if self._secure() else ""))

    def _client(self):
        return self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def _static(self, name, head_only):
        item = read_static(name)
        if item is None:
            return self._send(404, "Not found", "text/plain", head_only=head_only)
        raw, gz, ctype = item
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            return self._send(200, gz, ctype, [("Content-Encoding", "gzip"), ("Vary", "Accept-Encoding")], head_only, "public, max-age=86400")
        return self._send(200, raw, ctype, [("Vary", "Accept-Encoding")], head_only, "public, max-age=86400")

    def do_GET(self, head_only=False):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        cfg = self.server.cfg
        try:
            if path == "/healthz":  # "the website process is alive" - a database outage must not make Render restart-loop the service
                try:
                    conn = open_ro(cfg.db_path)
                    conn.execute("SELECT 1 FROM sessions LIMIT 1")
                    conn.close()
                    body = "ok"
                except (dbconn.DatabaseUnavailable, sqlite3.Error):
                    body = "ok (database unavailable)"
                return self._send(200, body, "text/plain", head_only=head_only)
            if path == "/favicon.ico":
                return self._send(204, "", "image/x-icon", head_only=head_only)
            if path.startswith("/static/"):
                return self._static(path[8:], head_only)
            ctx = Ctx(cfg, query)
            try:
                if ctx.session is None:
                    return self._send(200, error_page(None, 200, "Almost ready", "No challenge session has been created yet."), head_only=head_only)
                one = lambda k, d="": (query.get(k) or [d])[0]  # noqa: E731
                num = lambda k: int(one(k)) if one(k).isdigit() else None  # noqa: E731
                if path == "/":
                    html = page_home(ctx)
                elif path == "/leaderboard":
                    html = page_leaderboard(ctx, one("kind", "net"), num("page") or 0)
                elif path == "/streaks":
                    html = page_streaks(ctx)
                elif path == "/stats":
                    html = page_stats(ctx)
                elif path == "/calendar":
                    html = page_calendar(ctx, num("p"))
                elif path == "/participants":
                    html = page_participants(ctx)
                elif path == "/about":
                    html = page_about(ctx)
                elif path == "/settings":
                    html = page_settings(ctx)
                elif path.startswith("/participant/") and path[13:].isdigit():
                    html = page_participant(ctx, int(path[13:]))
                    if html is None:
                        return self._send(404, error_page(ctx, 404, "Participant not found", "That participant is not part of this session."), head_only=head_only)
                elif path == "/admin/login":
                    html = page_login(ctx)
                elif path == "/admin":
                    if not self._is_admin():
                        return self._send(302, "", headers=[("Location", "/admin/login")], head_only=head_only)
                    html = page_admin(ctx)
                elif path == "/admin/export.xlsx":
                    if not self._is_admin():
                        return self._send(302, "", headers=[("Location", "/admin/login")], head_only=head_only)
                    now = config.now()
                    data = export.build_workbook(ctx.conn, ctx.session, now, now.date())
                    self._audit("web_export", ctx.session["id"])
                    return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", [
                        ("Content-Disposition", f'attachment; filename="winter_arc_s{int(ctx.session["session_number"]):02d}_{now:%Y%m%d_%H%M}.xlsx"')], head_only)
                else:
                    return self._send(404, error_page(ctx, 404, "Page not found", "We couldn't find that page."), head_only=head_only)
                return self._send(200, html, head_only=head_only)
            finally:
                ctx.close()
        except (BrokenPipeError, ConnectionResetError):
            return
        except dbconn.DatabaseUnavailable:
            return self._send(503, error_page(None, 503, "Database temporarily unavailable", "The data could not be loaded right now. "
                                              "Nothing has been lost - please try again in a minute."), headers=[("Retry-After", "30")], head_only=head_only)
        except Exception:  # noqa: BLE001
            log.exception("Web request failed: %s", url.path)
            return self._send(500, error_page(None, 500, "Something went wrong", "The page could not be loaded. Please try again in a moment."), head_only=head_only)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        cfg = self.server.cfg
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 2048)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
            if path == "/admin/logout":
                return self._send(302, "", headers=[("Location", "/"), self._cookie("", 0)])
            if path != "/admin/login":
                return self._send(404, "Not found", "text/plain")
            ctx = Ctx(cfg, {})
            try:
                if not cfg.web_admin_password or ctx.session is None:
                    return self._send(403, page_login(ctx) if ctx.session else "disabled")
                ip, now = self._client(), time.time()
                with _attempts_lock:  # at most 5 wrong tries per IP per 5 minutes
                    tries = [t for t in _attempts.get(ip, []) if now - t < 300]
                    _attempts[ip] = tries
                    if len(tries) >= 5:
                        return self._send(429, page_login(ctx, "Too many attempts. Please wait a few minutes."))
                given = (form.get("password") or [""])[0].encode()
                if hmac.compare_digest(given, cfg.web_admin_password.encode()):
                    with _attempts_lock:
                        _attempts.pop(ip, None)
                    return self._send(302, "", headers=[("Location", "/admin"), self._cookie(make_token(self.server.secret), SESSION_HOURS * 3600)])
                with _attempts_lock:
                    _attempts[ip].append(now)
                log.warning("Wrong web admin password from %s", ip)
                return self._send(401, page_login(ctx, "That password isn't right."))
            finally:
                ctx.close()
        except dbconn.DatabaseUnavailable:
            return self._send(503, error_page(None, 503, "Database temporarily unavailable", "Please try again in a minute."), headers=[("Retry-After", "30")])
        except Exception:  # noqa: BLE001
            log.exception("Web POST failed")
            return self._send(500, error_page(None, 500, "Something went wrong", "Please try again."))

    def _audit(self, action, entity_id):
        """The website is read-only except for this one audit line (actor 0 = web admin)."""
        try:
            rw = db.connect(self.server.cfg.db_path)
            try:
                with db.transaction(rw):
                    db.audit(rw, 0, action, "session", entity_id)
            finally:
                rw.close()
        except Exception:  # noqa: BLE001
            log.exception("Could not write web audit entry")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.secret = cfg.web_secret or secrets.token_hex(32)  # without WEB_SECRET, logins end when the app restarts


def start_in_background(cfg, host="0.0.0.0", port=None) -> WebServer:
    server = WebServer((host, cfg.port if port is None else port), cfg)
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("Website listening on port %s", server.server_address[1])
    return server


def serve_forever(cfg):
    server = WebServer(("0.0.0.0", cfg.port), cfg)
    log.info("Website listening on port %s", cfg.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
