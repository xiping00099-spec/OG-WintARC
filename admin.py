"""
admin.py - the button-based admin panel plus advanced admin commands.

Security model: EVERY admin callback goes through one router (`admin_callback`) and every admin command
through `@guard`; both check the Telegram user ID against ADMIN_IDS on the server. Dangerous actions are
two-step: the first tap creates a one-time, expiring token (database table pending_actions) and the
second tap ("Confirm") executes the stored action exactly once.
"""
import asyncio
import functools
import io
import logging
import re
from datetime import timedelta

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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
import tg
from utils import (ValidationError, clean_text, esc, fmt_hours, fmt_minutes, parse_count, parse_duration)

log = logging.getLogger(__name__)
PAGE = 8
AWAIT_TTL = 600
EMOJI = {"GREEN": "🟩", "HALF": "🟨", "RED": "🟥", "PENDING": "🟧", "HEALTH": "🟦", "EMPTY": "⬜"}
KIND_LABEL = {"wasted": "Wasted day", "partial": "Partial day", "health": "Health day"}


# ----------------------------------------------------------------- guards & small helpers
def guard(fn):
    """Decorator for admin COMMANDS: server-side admin check, private chat only."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not tg.is_admin(context, user.id):
            log.warning("Blocked non-admin use of admin command by user %s", user.id if user else "?")
            if update.effective_message:
                await update.effective_message.reply_text("⛔ This command is for admins only.")
            return
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("🔒 Please use admin tools in a private chat with me.")
            return
        return await fn(update, context)
    return wrapper


async def show(update, text, markup=None):
    """Edit the pressed button's message, or send a new message for commands."""
    if update.callback_query:
        await tg.safe_edit(update.callback_query, text, markup)
    else:
        await tg.reply(update, text, markup)


def _ctx(context):
    conn = tg.conn_of(context)
    return conn, db.get_active_session(conn)


async def _no_session(update):
    await show(update, "There is no active session. Create one with /newsession or activate one with /sessions.")


def _page(items, page, size=PAGE):
    pages = max(1, -(-len(items) // size))
    page = max(0, min(page, pages - 1))
    return items[page * size:(page + 1) * size], page, pages


def _nav(prefix, page, pages):
    row = []
    if page > 0:
        row.append(("⬅️", f"{prefix}:{page - 1}"))
    if pages > 1:
        row.append((f"{page + 1}/{pages}", "a:noop"))
    if page < pages - 1:
        row.append(("➡️", f"{prefix}:{page + 1}"))
    return row


def recent_dates(session, n=7):
    start, end = reports.session_range(session)
    last = min(end, config.today())
    return [d for d in (last - timedelta(days=i) for i in range(n)) if d >= start]


async def notify(context, participant, text):
    if participant and participant["telegram_user_id"]:
        return await tg.safe_send(context.bot, participant["telegram_user_id"], text)
    return "blocked"


async def confirm_prompt(update, context, action, payload, text):
    """Show an 'Are you sure?' message backed by a one-time token."""
    conn = tg.conn_of(context)
    token = db.create_pending_action(conn, update.effective_user.id, action, payload)
    await show(update, f"⚠️ <b>Please confirm</b>\n\n{text}",
               tg.kb([[("✅ Confirm", f"cf:y:{token}"), ("❌ Cancel", f"cf:n:{token}")]]))


# ----------------------------------------------------------------- main menu & dashboard
async def h_menu(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    pend = len(reports.pending_reports(conn, session["id"]))
    await show(update, f"🛠 <b>Admin panel</b>\n{esc(reports.session_title(session))} · {reports.date_range_label(session)}",
               tg.kb([[("📊 Dashboard", "a:dash"), (f"📥 Pending reports ({pend})", "a:pend:0")],
                      [("👥 Participants", "a:parts:0"), ("📅 Daily attendance", f"a:att:{config.today().isoformat()}:0")],
                      [("🏆 Leaderboards", "lb:net:0"), ("⚖️ Apply penalty", "a:pn:0")],
                      [("🧾 Review penalties", "a:rev:0"), ("✏️ Correct a report", "a:cor:0")],
                      [("⚙️ Challenge settings", "a:set"), ("🖼 Generate chart", "a:chart")],
                      [("📤 Export records", "a:export")]]))


async def admin_cmd(update, context):
    await h_menu(update, context, [])


async def h_dash(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    sm = reports.build_summaries(conn, session, config.today())
    data = dashboard.dashboard_data(session, sm, config.today())
    await show(update, dashboard.dashboard_text(session, data),
               tg.kb([[("🖼 Dashboard image", "a:dashimg")], [("⬅️ Back", "a:menu")]]))


async def send_dashboard_image(update, context):
    conn, session = _ctx(context)
    chat = update.effective_chat.id
    if session is None:
        return
    if not tg.limiter.allow(("dashimg", chat), 20):
        return await context.bot.send_message(chat, "⏳ Please wait a few seconds before requesting another image.")
    today = config.today()
    data = dashboard.dashboard_data(session, reports.build_summaries(conn, session, today), today)
    png = await asyncio.to_thread(dashboard.render_dashboard, session, data, config.now())  # CPU work off the event loop
    await context.bot.send_photo(chat, io.BytesIO(png), caption=f"📊 {reports.session_title(session)} · "
                                 f"{reports.date_range_label(session)}")


async def send_chart(update, context, as_file=False):
    conn, session = _ctx(context)
    chat = update.effective_chat.id
    if session is None:
        return
    if not tg.limiter.allow(("chart", chat), 20):
        return await context.bot.send_message(chat, "⏳ Please wait a few seconds before requesting another chart.")
    today = config.today()
    size = int(db.get_setting(conn, "chart_page_size", 22))
    sm = reports.build_summaries(conn, session, today)
    pngs = await asyncio.to_thread(chart.render_attendance, session, sm, config.now(), size)
    for i, png in enumerate(pngs, 1):
        cap = f"{reports.session_title(session)} · page {i}/{len(pngs)}"
        if as_file:
            await context.bot.send_document(chat, io.BytesIO(png), filename=f"winter_arc_page{i}.png", caption=cap)
        else:
            await context.bot.send_photo(chat, io.BytesIO(png), caption=cap)


async def chart_cmd(update, context):  # public: built only from verified records
    await send_chart(update, context, as_file=bool(context.args and context.args[0] == "file"))


async def dashboard_cmd(update, context):
    await send_dashboard_image(update, context)


async def h_chart(update, context, args):
    await tg.safe_answer(update.callback_query, "Generating…")
    await send_chart(update, context)


async def h_dashimg(update, context, args):
    await tg.safe_answer(update.callback_query, "Generating…")
    await send_dashboard_image(update, context)


# ----------------------------------------------------------------- pending reports / review
def _report_text(conn, session, rep):
    part = ppl.get(conn, rep["participant_id"])
    pts = reports.provisional_points(rep, session)
    status_if = scoring.day_status("approved", reports.total_minutes(rep), reports.rules_of(session))
    names = f" ({esc(rep['exercise_names'])})" if rep["exercise_names"] else ""
    return (f"📝 <b>Report #{rep['id']}</b> — {esc(part['display_name'])} ({ppl.SCHOOL_LABELS[part['school_type']]})\n"
            f"Date: <b>{reports.parse_date(rep['report_date']).strftime('%a %d %b %Y')}</b> · status: <b>{rep['status'].replace('_', ' ')}</b> "
            f"· v{rep['version']}\nSubmitted: {rep['submitted_at'][:16].replace('T', ' ')}"
            + (" (entered by admin)" if rep["entered_by_admin"] else "") + "\n\n"
            f"Independent: {fmt_minutes(rep['independent_minutes'])} · School: {fmt_minutes(rep['school_minutes'])}\n"
            f"Exercise: {rep['exercise_count']}{names}\n"
            f"Physics {rep['physics_q']} · Chemistry {rep['chemistry_q']} · Biology {rep['biology_q']}\n"
            + (f"Notes: {esc(rep['notes'])}\n" if rep["notes"] else "")
            + (f"Review note: {esc(rep['review_note'])}\n" if rep["review_note"] else "")
            + f"\n<b>If approved:</b> {EMOJI[status_if]} {status_if.title()}\n{esc(pts.breakdown())}")


async def h_pend(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    items, page, pages = _page(reports.pending_reports(conn, session["id"]), int(args[0]))
    rows = [[(f"{r['display_name'][:18]} · {reports.parse_date(r['report_date']).strftime('%d %b')}", f"a:rep:{r['id']}")] for r in items]
    rows += [_nav("a:pend", page, pages), [("⬅️ Back", "a:menu")]]
    await show(update, "📥 <b>Reports awaiting approval</b>" + ("" if items else "\n\nNothing pending. 🎉"), tg.kb(rows))


async def h_rep(update, context, args):
    conn, session = _ctx(context)
    rep = reports.get_report_by_id(conn, int(args[0]))
    if rep is None or session is None:
        return await show(update, "That report no longer exists.", tg.kb([[("⬅️ Back", "a:pend:0")]]))
    rows = []
    if rep["status"] == "pending":
        rows.append([("✅ Approve", f"a:rv:{rep['id']}:ok")])
        rows.append([("✏️ Request correction", f"a:rv:{rep['id']}:cr"), ("❌ Reject", f"a:rv:{rep['id']}:no")])
    rows.append([("🛠 Correct values", f"a:cr:{rep['id']}"), ("⬅️ Back", "a:pend:0")])
    await show(update, _report_text(conn, session, rep), tg.kb(rows))


async def h_rv(update, context, args):
    conn, session = _ctx(context)
    rid, decision = int(args[0]), args[1]
    admin_id = update.effective_user.id
    if decision == "ok":
        try:
            rep = reports.review_report(conn, rid, admin_id, "approve")
        except ValidationError as e:
            return await show(update, f"⚠️ {esc(e)}", tg.kb([[("⬅️ Pending list", "a:pend:0")]]))
        part = ppl.get(conn, rep["participant_id"])
        pts = reports.provisional_points(rep, session)
        pen = reports.active_penalty(conn, part["id"], session["id"], reports.parse_date(rep["report_date"]))
        pen_line = f"\nPenalty: −{pen['amount']} ({esc(pen['reason'])})" if pen else ""
        net = pts.gross - (pen["amount"] if pen else 0)
        status = await notify(context, part, (
            f"✅ <b>Report approved</b> for {reports.parse_date(rep['report_date']).strftime('%d %b')}\n\n"
            f"{esc(pts.breakdown())}{pen_line}\nNet for the day: <b>{net}</b>"))
        note = "" if status == "sent" else "\n(Couldn't message the participant privately.)"
        zero = "\n⚠️ Zero study was recorded: use ⚖️ Apply penalty if this is a confirmed wasted day." if reports.total_minutes(rep) == 0 else ""
        return await show(update, f"✅ Approved report #{rid}.{note}{zero}", tg.kb([[("📥 Next pending", "a:pend:0")]]))
    rep = reports.get_report_by_id(conn, rid)
    if rep is None or rep["status"] != "pending":
        return await show(update, "⚠️ That report is no longer pending.", tg.kb([[("📥 Pending list", "a:pend:0")]]))
    context.user_data["await"] = {"kind": "review_note", "report_id": rid, "decision": "reject" if decision == "no" else "correction",
                                  "ts": config.now().timestamp()}
    word = "rejecting" if decision == "no" else "requesting a correction on"
    await show(update, f"✍️ Send the reason for {word} report #{rid} as your next message (or /cancel).")


# ----------------------------------------------------------------- participants
def _flag(row):
    return "" if row["is_active"] and row["member_active"] else " 🚫"


async def h_parts(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    members = ppl.list_members(conn, session["id"], only_active=False)
    items, page, pages = _page(members, int(args[0]))
    rows = [[(f"{m['display_name'][:24]}{_flag(m)}", f"a:p:{m['id']}")] for m in items]
    rows += [_nav("a:parts", page, pages), [("⬅️ Back", "a:menu")]]
    await show(update, f"👥 <b>Participants</b> ({len(members)})\n🚫 = inactive or removed from this session\n"
                       "Add people with /addp or /import.", tg.kb(rows))


async def h_p(update, context, args):
    conn, session = _ctx(context)
    pid = int(args[0])
    part = ppl.get(conn, pid)
    if part is None or session is None:
        return await show(update, "Participant not found.", tg.kb([[("⬅️ Back", "a:parts:0")]]))
    s = reports.get_summary(conn, session, pid, config.today())
    member = ppl.is_member(conn, session["id"], pid)
    blocked = conn.execute("SELECT 1 FROM reminders_sent WHERE participant_id=? AND status='blocked' LIMIT 1", (pid,)).fetchone()
    text = (f"👤 <b>{esc(part['display_name'])}</b> · ID {pid}\n"
            f"Telegram ID: {part['telegram_user_id'] or '— (admin-managed)'} · @{esc(part['username']) if part['username'] else 'no username'}\n"
            f"Type: {ppl.SCHOOL_LABELS[part['school_type']]} · Registered {part['registered_at'][:10]}\n"
            f"Profile: {'active' if part['is_active'] else '🚫 deactivated'} · This session: {'enrolled' if member else '🚫 not enrolled'}"
            + ("\n📵 Can't be messaged privately (hasn't started the bot)." if blocked else ""))
    if s:
        text += (f"\n\nNet <b>{s.net_points}</b> (gross {s.gross_points} − {s.penalty_total}) · {fmt_hours(s.verified_minutes)} · "
                 f"streak {s.current_streak}/{s.longest_streak}\n🟩{s.success_days} 🟨{s.partial_days} 🟥{s.unsuccessful_days} "
                 f"🟦{s.health_days} · pending {s.pending_reports} · missing {s.missing_reports}")
    rows = [[("✏️ Rename", f"a:pa:{pid}:ren"), ("🔁 Switch type", f"a:pa:{pid}:typ")],
            [("🔓 Reactivate", f"a:pa:{pid}:react") if not part["is_active"] else ("🚫 Deactivate", f"a:pa:{pid}:deact"),
             ("➕ Enroll in session", f"a:pa:{pid}:en") if not member else ("➖ Remove from session", f"a:pa:{pid}:rm")],
            [("🎨 Set day status", f"a:sd:{pid}"), ("⚖️ Penalty", f"a:pk:{pid}")],
            [("⬅️ Back", "a:parts:0")]]
    await show(update, text, tg.kb(rows))


async def h_pa(update, context, args):
    conn, session = _ctx(context)
    pid, act = int(args[0]), args[1]
    part = ppl.get(conn, pid)
    if part is None or session is None:
        return await show(update, "Participant not found.")
    name = esc(part["display_name"])
    if act == "ren":
        context.user_data["await"] = {"kind": "rename", "pid": pid, "ts": config.now().timestamp()}
        return await show(update, f"✍️ Send the new display name for <b>{name}</b> (or /cancel).")
    if act == "typ":
        new = "school" if part["school_type"] == "dummy" else "dummy"
        return await confirm_prompt(update, context, "set_type", {"pid": pid, "type": new},
                                    f"Change <b>{name}</b> to <b>{ppl.SCHOOL_LABELS[new]}</b>?")
    texts = {"deact": f"Deactivate <b>{name}</b>? They leave leaderboards and can't submit. All records are kept.",
             "react": f"Reactivate <b>{name}</b>?",
             "rm": f"Remove <b>{name}</b> from {esc(reports.session_title(session))}? Their records stay in the database.",
             "en": f"Enroll <b>{name}</b> in {esc(reports.session_title(session))}?"}
    if act not in texts:
        return
    await confirm_prompt(update, context, {"deact": "deactivate", "react": "reactivate", "rm": "session_remove", "en": "session_enroll"}[act],
                         {"pid": pid, "session_id": session["id"]}, texts[act])


# ----------------------------------------------------------------- daily attendance
async def h_att(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    day, page = reports.parse_date(args[0]), int(args[1])
    start, end = reports.session_range(session)
    day = max(start, min(day, end))
    sm = sorted(reports.build_summaries(conn, session, config.today()), key=lambda s: (s.name.casefold(), s.participant_id))
    items, page, pages = _page(sm, page, 25)
    lines = []
    for s in items:
        r = s.days[day]
        extra = fmt_minutes(r.minutes) if r.minutes else (r.report_status.replace("_", " ") if r.report_status else "")
        lines.append(f"{EMOJI[r.status]} {esc(s.name)}" + (f" — {extra}" if extra else ""))
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    nav = []
    if prev_d >= start:
        nav.append(("◀️ " + prev_d.strftime("%d %b"), f"a:att:{prev_d.isoformat()}:0"))
    if next_d <= end:
        nav.append((next_d.strftime("%d %b") + " ▶️", f"a:att:{next_d.isoformat()}:0"))
    await show(update, f"📅 <b>Attendance · {day.strftime('%A %d %b %Y')}</b>\n🟩 full 🟨 partial 🟥 failed 🟧 pending 🟦 health ⬜ none\n\n"
                       + ("\n".join(lines) or "No participants."),
               tg.kb([nav, _nav(f"a:att:{day.isoformat()}", page, pages), [("⬅️ Back", "a:menu")]]))


# ----------------------------------------------------------------- penalties
async def h_pn(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    members = ppl.list_members(conn, session["id"])
    items, page, pages = _page(members, int(args[0]))
    rows = [[(m["display_name"][:28], f"a:pk:{m['id']}")] for m in items] + [_nav("a:pn", page, pages), [("⬅️ Back", "a:menu")]]
    await show(update, "⚖️ <b>Apply penalty</b>\nChoose the participant:", tg.kb(rows))


async def h_pk(update, context, args):
    conn, session = _ctx(context)
    pid = int(args[0])
    part = ppl.get(conn, pid)
    if part is None or session is None:
        return await show(update, "Participant not found.")
    rows = [[(f"{KIND_LABEL['wasted']} (−{session['penalty_wasted']})", f"a:pt:{pid}:wasted")],
            [(f"{KIND_LABEL['partial']} (−{session['penalty_partial']})", f"a:pt:{pid}:partial")],
            [(f"{KIND_LABEL['health']} (" + (f"−{session['penalty_health']})" if session["health_penalty_confirmed"] else "policy off)"),
              f"a:pt:{pid}:health")], [("⬅️ Back", "a:pn:0")]]
    await show(update, f"⚖️ Penalty for <b>{esc(part['display_name'])}</b> — which type?", tg.kb(rows))


async def h_pt(update, context, args):
    conn, session = _ctx(context)
    pid, kind = int(args[0]), args[1]
    if kind not in reports.PENALTY_KINDS or session is None:
        return
    if kind == "health" and not session["health_penalty_confirmed"]:
        return await show(update, "ℹ️ The health-day penalty is <b>off</b>: the group hasn't confirmed it as final policy. "
                                  "Use 🎨 Set day status → HEALTH to record the day without a deduction, or enable the policy "
                                  "in ⚙️ settings.", tg.kb([[("⬅️ Back", f"a:pk:{pid}")]]))
    rows = [[(d.strftime("%a %d %b"), f"a:pc:{pid}:{kind}:{d.isoformat()}")] for d in recent_dates(session)]
    await show(update, "📅 Which date?", tg.kb(rows + [[("⬅️ Back", f"a:pk:{pid}")]]))


async def h_pc(update, context, args):
    conn, session = _ctx(context)
    pid, kind, day = int(args[0]), args[1], reports.parse_date(args[2])
    part = ppl.get(conn, pid)
    if part is None or session is None:
        return
    await confirm_prompt(update, context, "apply_penalty", {"pid": pid, "kind": kind, "date": day.isoformat(), "session_id": session["id"]},
                         f"Apply <b>−{reports.penalty_amount(session, kind)}</b> ({KIND_LABEL[kind].lower()}) to "
                         f"<b>{esc(part['display_name'])}</b> for <b>{day.strftime('%d %b %Y')}</b>?")


async def h_rev(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    pens = reports.list_penalties(conn, session["id"])
    items, page, pages = _page(pens, int(args[0]))
    rows = [[(f"#{p['id']} {p['display_name'][:14]} {p['penalty_date'][5:]} −{p['amount']}" + (" ↩️" if p["status"] == "reversed" else ""),
              f"a:pv:{p['id']}")] for p in items]
    await show(update, "🧾 <b>Penalty ledger</b> (↩️ = reversed)" + ("" if pens else "\n\nNo penalties recorded."),
               tg.kb(rows + [_nav("a:rev", page, pages), [("⬅️ Back", "a:menu")]]))


async def h_pv(update, context, args):
    conn, session = _ctx(context)
    p = conn.execute("SELECT pe.*, pa.display_name FROM penalties pe JOIN participants pa ON pa.id=pe.participant_id WHERE pe.id=?",
                     (int(args[0]),)).fetchone()
    if p is None:
        return await show(update, "Penalty not found.", tg.kb([[("⬅️ Back", "a:rev:0")]]))
    text = (f"🧾 <b>Penalty #{p['id']}</b> — {esc(p['display_name'])}\nDate {p['penalty_date']} · {KIND_LABEL[p['kind']]} · <b>−{p['amount']}</b>\n"
            f"Reason: {esc(p['reason'])}\nBy: {'system (automatic)' if p['is_auto'] else 'admin ' + str(p['admin_id'])} · {p['created_at'][:16].replace('T', ' ')}\n"
            f"Status: <b>{p['status']}</b>")
    if p["status"] == "reversed":
        text += f"\nReversed by {'system' if p['reversed_by'] == 0 else 'admin ' + str(p['reversed_by'])}: {esc(p['reversal_reason'])}"
    rows = [[("↩️ Reverse", f"a:pr:{p['id']}")]] if p["status"] == "active" else []
    await show(update, text, tg.kb(rows + [[("⬅️ Back", "a:rev:0")]]))


async def h_pr(update, context, args):
    context.user_data["await"] = {"kind": "penalty_reverse", "penalty_id": int(args[0]), "ts": config.now().timestamp()}
    await show(update, f"✍️ Send the reason for reversing penalty #{args[0]} (or /cancel).")


# ----------------------------------------------------------------- day status override
async def h_sd(update, context, args):
    conn, session = _ctx(context)
    pid = int(args[0])
    rows = [[(d.strftime("%a %d %b"), f"a:sl:{pid}:{d.isoformat()}")] for d in recent_dates(session)]
    await show(update, "🎨 <b>Set day status</b> — which date?", tg.kb(rows + [[("⬅️ Back", f"a:p:{pid}")]]))


async def h_sl(update, context, args):
    pid, day = int(args[0]), args[1]
    names = [("🟩 Green", "GREEN"), ("🟨 Half", "HALF"), ("🟥 Red", "RED"), ("🟦 Health", "HEALTH"), ("⬜ Empty", "EMPTY"), ("🧹 Clear override", "CLEAR")]
    rows = [[(n, f"a:ss:{pid}:{day}:{c}") for n, c in names[i:i + 3]] for i in (0, 3)]
    await show(update, f"🎨 Status for <b>{day}</b>?\nThis only changes the colour/status — points come from approved reports and the penalty ledger.",
               tg.kb(rows + [[("⬅️ Back", f"a:sd:{pid}")]]))


async def h_ss(update, context, args):
    context.user_data["await"] = {"kind": "status_reason", "pid": int(args[0]), "date": args[1], "status": args[2],
                                  "ts": config.now().timestamp()}
    await show(update, f"✍️ Send the reason for setting {args[2]} on {args[1]} (it is recorded in the audit log), or /cancel.")


# ----------------------------------------------------------------- correct a report
FIELDS = {"ind": ("independent_minutes", "Independent study", "dur"), "sch": ("school_minutes", "School study", "dur"),
          "exc": ("exercise_count", "Exercise count", "cnt"), "phy": ("physics_q", "Physics questions", "cnt"),
          "che": ("chemistry_q", "Chemistry questions", "cnt"), "bio": ("biology_q", "Biology questions", "cnt"),
          "names": ("exercise_names", "Exercise names", "txt"), "notes": ("notes", "Notes", "txt")}


async def h_cor(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    items, page, pages = _page(reports.recent_reports(conn, session["id"]), int(args[0]))
    rows = [[(f"{r['display_name'][:16]} · {r['report_date'][5:]} · {r['status'][:4]}", f"a:cr:{r['id']}")] for r in items]
    await show(update, "✏️ <b>Correct a report</b>\nPick a report:", tg.kb(rows + [_nav("a:cor", page, pages), [("⬅️ Back", "a:menu")]]))


async def h_cr(update, context, args):
    conn, session = _ctx(context)
    rep = reports.get_report_by_id(conn, int(args[0]))
    if rep is None:
        return await show(update, "Report not found.", tg.kb([[("⬅️ Back", "a:cor:0")]]))
    f = [(FIELDS[k][1], f"a:cf:{rep['id']}:{k}") for k in FIELDS]
    rows = [f[i:i + 2] for i in range(0, len(f), 2)] + [[("⬅️ Back", "a:cor:0")]]
    await show(update, _report_text(conn, session, rep) + "\n\n<b>Which value needs correcting?</b>\nPoints are recalculated, never added twice.", tg.kb(rows))


async def h_cf(update, context, args):
    col, label, _ = FIELDS[args[1]]
    context.user_data["await"] = {"kind": "report_field", "report_id": int(args[0]), "field": args[1], "ts": config.now().timestamp()}
    hint = {"dur": "e.g. 7:30, 7h30m or 450m", "cnt": "a whole number", "txt": "text, or - to clear"}[FIELDS[args[1]][2]]
    await show(update, f"✍️ Send the new value for <b>{label}</b> ({hint}), or /cancel.")


# ----------------------------------------------------------------- settings
SETTING_INPUTS = {  # button key -> (target, column/key, kind, label)
    "threshold": ("session", "full_green_minutes", "dur", "Full-green threshold"),
    "tolerance": ("session", "tolerance_minutes", "tol", "Tolerance (minutes)"),
    "deadline": ("session", "target_deadline", "time", "Target deadline (HH:MM IST)"),
    "window": ("session", "report_window_days", "win", "Report window (days)"),
    "p_wasted": ("session", "penalty_wasted", "pen", "Wasted-day penalty"),
    "p_partial": ("session", "penalty_partial", "pen", "Partial-day penalty"),
    "p_health": ("session", "penalty_health", "pen", "Health-day penalty"),
    "chart_page": ("bot", "chart_page_size", "page", "Participants per chart page"),
    "rem_morning_time": ("bot", "rem_morning_time", "time", "Morning reminder time"),
    "rem_evening_time": ("bot", "rem_evening_time", "time", "Evening reminder time"),
    "rem_correction_time": ("bot", "rem_correction_time", "time", "Correction reminder time"),
    "rem_admin_time": ("bot", "rem_admin_time", "time", "Admin reminder time"),
}
REMINDERS = [("morning", "Morning targets"), ("evening", "Evening report"), ("correction", "Corrections"), ("admin", "Admin pending")]


def parse_setting(kind, text):
    t = text.strip()
    if kind == "dur":
        m = parse_duration(t)
        if not 60 <= m <= 1440:
            raise ValidationError("Threshold must be between 1h and 24h.")
        return m
    if kind == "tol":
        return parse_count(t, 59, "Tolerance")
    if kind == "time":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValidationError("Use 24-hour HH:MM, for example 09:00.")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if kind == "win":
        n = parse_count(t, 14, "Window")
        if n < 1:
            raise ValidationError("Window must be at least 1 day.")
        return n
    if kind == "pen":
        return parse_count(t, 50, "Penalty")
    if kind == "page":
        n = parse_count(t, 40, "Page size")
        if n < 5:
            raise ValidationError("Use at least 5 participants per page.")
        return n
    raise ValueError(kind)


async def h_set(update, context, args):
    conn, session = _ctx(context)
    if session is None:
        return await _no_session(update)
    r = reports.rules_of(session)
    text = (f"⚙️ <b>Challenge settings</b>\n{esc(reports.session_title(session))} · {reports.date_range_label(session)}\n\n"
            f"Full-green threshold: <b>{fmt_minutes(r.full_green_minutes)}</b>\nTolerance: <b>{r.tolerance_minutes} min</b> · rounding: <b>{r.rounding_policy}</b>\n"
            f"Penalties: wasted −{r.penalty_wasted} · partial −{r.penalty_partial} ({'ON' if r.apply_partial_penalty else 'OFF'}) · "
            f"health −{r.penalty_health} ({'ON' if r.health_penalty_confirmed else 'OFF — not confirmed policy'})\n"
            f"Target deadline: <b>{session['target_deadline']} IST</b> · report window: {session['report_window_days']} day(s)\n"
            f"Chart page size: {db.get_setting(conn, 'chart_page_size', 22)}\n\n"
            "<i>Changing rules recalculates points for the whole session, because points are always derived from approved reports.</i>")
    rows = [[("⏱ Threshold", "a:se:threshold"), ("🎯 Tolerance", "a:se:tolerance")],
            [("🔁 Rounding policy", "a:tg:rounding"), ("⏰ Deadline", "a:se:deadline")],
            [("−Wasted", "a:se:p_wasted"), ("−Partial", "a:se:p_partial"), ("−Health", "a:se:p_health")],
            [("Partial penalty: " + ("ON" if r.apply_partial_penalty else "OFF"), "a:tg:partial"),
             ("Health penalty: " + ("ON" if r.health_penalty_confirmed else "OFF"), "a:tg:health")],
            [("🗓 Report window", "a:se:window"), ("📄 Chart page size", "a:se:chart_page")],
            [("🔔 Reminders", "a:rem"), ("🗃 Sessions", "a:sess")], [("⬅️ Back", "a:menu")]]
    await show(update, text, tg.kb(rows))


async def h_se(update, context, args):
    target, col, kind, label = SETTING_INPUTS[args[0]]
    context.user_data["await"] = {"kind": "setting", "key": args[0], "ts": config.now().timestamp()}
    await show(update, f"✍️ Send the new value for <b>{label}</b>, or /cancel.")


async def h_tg(update, context, args):
    conn, session = _ctx(context)
    which = args[0]
    if which == "rounding":
        new = scoring.ROUND_FLOOR if session["rounding_policy"] == scoring.ROUND_TOLERANCE else scoring.ROUND_TOLERANCE
        payload = {"target": "session", "key": "rounding_policy", "value": new, "label": "Rounding policy"}
    elif which == "partial":
        payload = {"target": "session", "key": "apply_partial_penalty", "value": 0 if session["apply_partial_penalty"] else 1, "label": "Partial-day penalty"}
    else:
        payload = {"target": "session", "key": "health_penalty_confirmed", "value": 0 if session["health_penalty_confirmed"] else 1,
                   "label": "Health-day penalty (final group policy confirmed)"}
    payload["session_id"] = session["id"]
    await confirm_prompt(update, context, "set_setting", payload, f"Set <b>{payload['label']}</b> to <b>{payload['value']}</b>?\nPoints for the whole session will be recalculated.")


async def h_rem(update, context, args):
    conn, _ = _ctx(context)
    rows = []
    for key, label in REMINDERS:
        on = db.get_setting(conn, f"rem_{key}_enabled", "1") == "1"
        rows.append([(f"{label}: {'ON' if on else 'OFF'}", f"a:st:rem_{key}_enabled"),
                     (f"⏰ {db.get_setting(conn, f'rem_{key}_time')}", f"a:se:rem_{key}_time")])
    await show(update, "🔔 <b>Reminders</b> (Asia/Kolkata)\nTap a name to switch it on/off, or the time to change it.",
               tg.kb(rows + [[("⬅️ Back", "a:set")]]))


async def h_st(update, context, args):
    conn, _ = _ctx(context)
    key = args[0]
    if not re.fullmatch(r"rem_(morning|evening|correction|admin)_enabled", key):
        return
    new = "0" if db.get_setting(conn, key, "1") == "1" else "1"
    with db.transaction(conn):
        db.set_setting(conn, key, new)
        db.audit(conn, update.effective_user.id, "reminder_toggle", "setting", key, {"value": new})
    await h_rem(update, context, [])


async def h_sess(update, context, args):
    conn, session = _ctx(context)
    rows, lines = [], []
    for s in db.list_sessions(conn):
        state = "ACTIVE" if s["is_active"] else ("archived" if s["archived"] else "inactive")
        lines.append(f"#{s['id']} {esc(reports.session_title(s))} · {state}")
        if not s["is_active"]:
            rows.append([(f"▶️ Activate #{s['id']}", f"a:ac:{s['id']}")])
    if session:
        rows.insert(0, [("📦 Archive current session", "a:arch")])
    await show(update, "🗃 <b>Sessions</b>\n" + "\n".join(lines) + "\n\nNew session: /newsession &lt;number&gt; &lt;start&gt; &lt;end&gt; [copy]",
               tg.kb(rows + [[("⬅️ Back", "a:set")]]))


async def h_arch(update, context, args):
    conn, session = _ctx(context)
    if session:
        await confirm_prompt(update, context, "archive_session", {"session_id": session["id"]},
                             f"Archive <b>{esc(reports.session_title(session))}</b>? All its data is kept and can be viewed/exported later, "
                             "but no new reports are accepted until a session is active.")


async def h_ac(update, context, args):
    conn, _ = _ctx(context)
    s = db.get_session(conn, int(args[0]))
    if s:
        await confirm_prompt(update, context, "activate_session", {"session_id": s["id"]}, f"Make <b>{esc(reports.session_title(s))}</b> the active session?")


# ----------------------------------------------------------------- export
async def send_export(update, context):
    conn, session = _ctx(context)
    if session is None:
        return
    chat = update.effective_chat.id
    if not tg.limiter.allow(("export", chat), 15):
        return await context.bot.send_message(chat, "⏳ Please wait a few seconds before exporting again.")
    now = config.now()
    data = export.build_workbook(conn, session, now, now.date())
    with db.transaction(conn):
        db.audit(conn, update.effective_user.id, "export", "session", session["id"])
    fname = f"winter_arc_s{int(session['session_number']):02d}_{now.strftime('%Y%m%d_%H%M')}.xlsx"
    await context.bot.send_document(chat, io.BytesIO(data), filename=fname, caption=f"📤 {reports.session_title(session)} export")


async def h_export(update, context, args):
    await tg.safe_answer(update.callback_query, "Preparing export…")
    await send_export(update, context)


@guard
async def export_cmd(update, context):
    await send_export(update, context)


async def h_noop(update, context, args):
    return


ROUTES = {"menu": h_menu, "dash": h_dash, "dashimg": h_dashimg, "chart": h_chart, "pend": h_pend, "rep": h_rep, "rv": h_rv,
          "parts": h_parts, "p": h_p, "pa": h_pa, "att": h_att, "pn": h_pn, "pk": h_pk, "pt": h_pt, "pc": h_pc, "rev": h_rev,
          "pv": h_pv, "pr": h_pr, "sd": h_sd, "sl": h_sl, "ss": h_ss, "cor": h_cor, "cr": h_cr, "cf": h_cf, "set": h_set,
          "se": h_se, "tg": h_tg, "rem": h_rem, "st": h_st, "sess": h_sess, "arch": h_arch, "ac": h_ac, "export": h_export,
          "noop": h_noop}


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point for every 'a:' button. Checks admin rights BEFORE doing anything."""
    q = update.callback_query
    if not tg.is_admin(context, update.effective_user.id):
        log.warning("Blocked non-admin button press from %s", update.effective_user.id)
        return await tg.safe_answer(q, "⛔ Admins only.", show_alert=True)
    if update.effective_chat.type != "private":
        return await tg.safe_answer(q, "Please use the admin panel in a private chat.", show_alert=True)
    parts = q.data.split(":")[1:]
    handler = ROUTES.get(parts[0] if parts else "")
    if handler is None:
        return await tg.safe_answer(q, "This button is no longer valid.", show_alert=True)
    await tg.safe_answer(q)
    context.user_data.pop("await", None)  # opening any panel page cancels a half-finished text prompt
    try:
        await handler(update, context, parts[1:])
    except (ValueError, IndexError, KeyError):
        log.exception("Bad admin callback %r", q.data)
        await tg.safe_answer(q, "Invalid or outdated button.", show_alert=True)
    except ValidationError as e:
        await show(update, f"⚠️ {esc(e)}", tg.kb([[("⬅️ Admin menu", "a:menu")]]))


# ----------------------------------------------------------------- confirmed actions (run once, after "Confirm")
async def act_apply_penalty(conn, context, admin_id, p):
    session = db.get_session(conn, p["session_id"])
    part = ppl.get(conn, p["pid"])
    day = reports.parse_date(p["date"])
    pen_id = reports.apply_penalty(conn, p["pid"], session, day, p["kind"], admin_id, p.get("reason"))
    amount = reports.penalty_amount(session, p["kind"])
    await notify(context, part, f"⚖️ A penalty of <b>−{amount}</b> ({KIND_LABEL[p['kind']].lower()}) was recorded for {day.strftime('%d %b')}.")
    return f"✅ Penalty #{pen_id} applied to {esc(part['display_name'])} (−{amount})."


async def act_reverse_penalty(conn, context, admin_id, p):
    row = reports.reverse_penalty(conn, p["penalty_id"], admin_id, p["reason"])
    return f"✅ Penalty #{row['id']} reversed. Reason saved in the audit log."


async def act_deactivate(conn, context, admin_id, p):
    ppl.set_active(conn, p["pid"], False, admin_id)
    return "✅ Participant deactivated. Their history is kept."


async def act_reactivate(conn, context, admin_id, p):
    ppl.set_active(conn, p["pid"], True, admin_id)
    return "✅ Participant reactivated."


async def act_session_remove(conn, context, admin_id, p):
    with db.transaction(conn):
        ppl.remove_from_session(conn, p["session_id"], p["pid"])
        db.audit(conn, admin_id, "session_remove", "participant", p["pid"], {"session": p["session_id"]})
    return "✅ Removed from the session (records kept)."


async def act_session_enroll(conn, context, admin_id, p):
    with db.transaction(conn):
        ppl.enroll(conn, p["session_id"], p["pid"])
        db.audit(conn, admin_id, "session_enroll", "participant", p["pid"], {"session": p["session_id"]})
    return "✅ Enrolled in the session."


async def act_set_type(conn, context, admin_id, p):
    ppl.update(conn, p["pid"], admin_id, school_type=p["type"])
    return f"✅ Type changed to {ppl.SCHOOL_LABELS[p['type']]}."


async def act_edit_report(conn, context, admin_id, p):
    rep = reports.admin_edit_report(conn, p["report_id"], admin_id, p["changes"], p.get("reason", "Admin correction via panel"))
    part = ppl.get(conn, rep["participant_id"])
    await notify(context, part, f"✏️ An admin corrected your report for {reports.parse_date(rep['report_date']).strftime('%d %b')}. "
                                "Check /points for the updated breakdown.")
    return f"✅ Report #{rep['id']} updated; points recalculated (not added on top)."


async def act_set_setting(conn, context, admin_id, p):
    with db.transaction(conn):
        if p["target"] == "session":
            old = db.get_session(conn, p["session_id"])[p["key"]]
            db.update_session(conn, p["session_id"], **{p["key"]: p["value"]})
            reports.resync_partial_penalties(conn, p["session_id"])
        else:
            old = db.get_setting(conn, p["key"])
            db.set_setting(conn, p["key"], p["value"])
        db.audit(conn, admin_id, "setting_change", p["target"], p["key"], {"old": old, "new": p["value"]})
    return f"✅ {esc(p['label'])} updated."


async def act_archive_session(conn, context, admin_id, p):
    with db.transaction(conn):
        db.archive_session(conn, p["session_id"])
        db.audit(conn, admin_id, "session_archive", "session", p["session_id"])
    return "📦 Session archived. All data is preserved."


async def act_activate_session(conn, context, admin_id, p):
    db.activate_session(conn, p["session_id"])
    with db.transaction(conn):
        db.audit(conn, admin_id, "session_activate", "session", p["session_id"])
    return "✅ Session activated."


async def act_set_status(conn, context, admin_id, p):
    session = db.get_session(conn, p["session_id"])
    day = reports.parse_date(p["date"])
    if p["status"] == "CLEAR":
        reports.clear_day_status(conn, p["pid"], session, day, admin_id, p["reason"])
    else:
        reports.set_day_status(conn, p["pid"], session, day, p["status"], admin_id, p["reason"])
    return f"✅ Day status for {p['date']}: {p['status']}. Recorded in the audit log."


async def act_new_session(conn, context, admin_id, p):
    old = db.get_active_session(conn)
    with db.transaction(conn):
        sid = db.create_session(conn, p["name"], p["number"], p["group"], p["start"], p["end"],
                                copy_rules_from=p.get("copy_from"), make_active=True)
        if p.get("copy_members") and old:
            for m in ppl.list_members(conn, old["id"]):
                ppl.enroll(conn, sid, m["id"])
        db.audit(conn, admin_id, "session_create", "session", sid, p)
    return f"✅ Session #{sid} created and activated. The previous session is untouched."


ACTIONS = {"apply_penalty": act_apply_penalty, "reverse_penalty": act_reverse_penalty, "deactivate": act_deactivate,
           "reactivate": act_reactivate, "session_remove": act_session_remove, "session_enroll": act_session_enroll,
           "set_type": act_set_type, "edit_report": act_edit_report, "set_setting": act_set_setting,
           "archive_session": act_archive_session, "activate_session": act_activate_session,
           "set_status": act_set_status, "new_session": act_new_session}


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not tg.is_admin(context, update.effective_user.id):
        return await tg.safe_answer(q, "⛔ Admins only.", show_alert=True)
    _, choice, token = q.data.split(":", 2)
    conn = tg.conn_of(context)
    got = db.consume_pending_action(conn, token, update.effective_user.id)  # one-time, expiring, tied to this admin
    if got is None:
        await tg.safe_answer(q, "This confirmation expired or was already used.", show_alert=True)
        return await tg.safe_edit(q, "⌛ This confirmation expired or was already used.", tg.kb([[("⬅️ Admin menu", "a:menu")]]))
    await tg.safe_answer(q)
    if choice == "n":
        return await tg.safe_edit(q, "❌ Cancelled. Nothing was changed.", tg.kb([[("⬅️ Admin menu", "a:menu")]]))
    action, payload = got
    try:
        msg = await ACTIONS[action](conn, context, update.effective_user.id, payload)
    except ValidationError as e:
        msg = f"⚠️ {esc(e)}"
    except Exception:  # noqa: BLE001
        log.exception("Confirmed action %s failed", action)
        msg = "⚠️ Something went wrong; nothing was saved. Details are in the log."
    await tg.safe_edit(q, msg, tg.kb([[("⬅️ Admin menu", "a:menu")]]))


# ----------------------------------------------------------------- typed answers to panel prompts
async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aw = context.user_data.get("await")
    if not aw or not tg.is_admin(context, update.effective_user.id):
        return
    if config.now().timestamp() - aw["ts"] > AWAIT_TTL:
        context.user_data.pop("await", None)
        return await tg.reply(update, "⌛ That prompt expired. Open the panel again with /admin.")
    text = (update.effective_message.text or "").strip()
    conn, session = _ctx(context)
    admin_id = update.effective_user.id
    try:
        kind = aw["kind"]
        if kind == "review_note":
            rep = reports.review_report(conn, aw["report_id"], admin_id, aw["decision"], text)
            part = ppl.get(conn, rep["participant_id"])
            what = "rejected" if aw["decision"] == "reject" else "needs a correction"
            await notify(context, part, f"⚠️ Your report for {reports.parse_date(rep['report_date']).strftime('%d %b')} {what}.\n"
                                        f"Reason: {esc(text)}\nUse /report to resubmit.")
            await tg.reply(update, f"✅ Report #{rep['id']} marked {rep['status'].replace('_', ' ')}.", tg.kb([[("📥 Pending list", "a:pend:0")]]))
        elif kind == "rename":
            ppl.update(conn, aw["pid"], admin_id, display_name=text)
            await tg.reply(update, "✅ Renamed.", tg.kb([[("👤 Back to profile", f"a:p:{aw['pid']}")]]))
        elif kind == "penalty_reverse":
            reason = clean_text(text, 300, "Reason", allow_empty=False)
            await confirm_prompt(update, context, "reverse_penalty", {"penalty_id": aw["penalty_id"], "reason": reason},
                                 f"Reverse penalty #{aw['penalty_id']}?\nReason: {esc(reason)}")
        elif kind == "status_reason":
            reason = clean_text(text, 300, "Reason", allow_empty=False)
            await confirm_prompt(update, context, "set_status", {"pid": aw["pid"], "date": aw["date"], "status": aw["status"],
                                                                 "reason": reason, "session_id": session["id"]},
                                 f"Set status <b>{aw['status']}</b> for {aw['date']}?\nReason: {esc(reason)}")
        elif kind == "report_field":
            col, label, ftype = FIELDS[aw["field"]]
            value = (parse_duration(text) if ftype == "dur" else parse_count(text, 1000, label) if ftype == "cnt"
                     else ("" if text == "-" else clean_text(text, 500, label)))
            rep = reports.get_report_by_id(conn, aw["report_id"])
            await confirm_prompt(update, context, "edit_report", {"report_id": rep["id"], "changes": {col: value}},
                                 f"Change <b>{label}</b> on report #{rep['id']} from <b>{esc(rep[col])}</b> to <b>{esc(value)}</b>?\n"
                                 "Points will be recalculated, not added twice.")
        elif kind == "setting":
            target, col, ftype, label = SETTING_INPUTS[aw["key"]]
            value = parse_setting(ftype, text)
            await confirm_prompt(update, context, "set_setting", {"target": target, "key": col, "value": value, "label": label,
                                                                  "session_id": session["id"]},
                                 f"Set <b>{label}</b> to <b>{esc(value)}</b>?" + ("\nPoints for the whole session will be recalculated." if target == "session" else ""))
        context.user_data.pop("await", None)
    except ValidationError as e:  # keep the prompt open so the admin can retype
        await tg.reply(update, f"⚠️ {esc(e)}\nTry again or send /cancel.")


async def cancel_cmd(update, context):
    had = context.user_data.pop("await", None) or context.user_data.pop("flow", None)
    await tg.reply(update, "❌ Cancelled." if had else "Nothing to cancel.")


# ----------------------------------------------------------------- advanced admin commands
@guard
async def addp_cmd(update, context):
    conn, session = _ctx(context)
    parts = [x.strip() for x in " ".join(context.args).split("|")]
    if not parts[0] or session is None:
        return await tg.reply(update, "Use: /addp Name | school|dummy | telegram_id (optional)")
    typ = (parts[1] if len(parts) > 1 and parts[1] else "dummy").lower()
    tgid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    try:
        row = ppl.register(conn, parts[0], "school" if typ.startswith("school") else "dummy", session["id"],
                           telegram_user_id=tgid, registered_by=update.effective_user.id)
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await tg.reply(update, f"✅ Added <b>{esc(row['display_name'])}</b> with ID {row['id']}.")


@guard
async def import_cmd(update, context):
    conn, session = _ctx(context)
    lines = (update.effective_message.text or "").split("\n")[1:]
    if not lines or session is None:
        return await tg.reply(update, "Send /import followed by one participant per line:\nName | school|dummy | telegram_id (optional)")
    ok, bad = 0, []
    for ln in lines:
        p = [x.strip() for x in ln.split("|")]
        if not p[0]:
            continue
        try:
            tgid = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
            ppl.register(conn, p[0], "school" if len(p) > 1 and p[1].lower().startswith("school") else "dummy", session["id"],
                         telegram_user_id=tgid, registered_by=update.effective_user.id)
            ok += 1
        except ValidationError as e:
            bad.append(f"{esc(p[0])}: {esc(e)}")
    await tg.reply(update, f"✅ Imported {ok}." + ("\n⚠️ Skipped:\n" + "\n".join(bad) if bad else ""))


@guard
async def editp_cmd(update, context):
    conn, _ = _ctx(context)
    try:
        pid, field, value = int(context.args[0]), context.args[1].lower(), " ".join(context.args[2:])
        ppl.update(conn, pid, update.effective_user.id, **{"display_name" if field == "name" else "school_type": value.lower() if field == "type" else value})
    except (IndexError, ValueError):
        return await tg.reply(update, "Use: /editp &lt;id&gt; name|type &lt;value&gt;")
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await tg.reply(update, "✅ Updated.")


@guard
async def link_cmd(update, context):
    conn, _ = _ctx(context)
    try:
        ppl.link_telegram(conn, int(context.args[0]), int(context.args[1]), update.effective_user.id)
    except (IndexError, ValueError):
        return await tg.reply(update, "Use: /link &lt;participant id&gt; &lt;telegram user id&gt;")
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await tg.reply(update, "✅ Linked.")


@guard
async def logreport_cmd(update, context):
    """/logreport <id> <date> <independent> <school> <exercises> <phy> <chem> <bio>  (admin-entered, auto-approved)"""
    conn, session = _ctx(context)
    try:
        a = context.args
        part = ppl.get(conn, int(a[0]))
        data = dict(independent_minutes=parse_duration(a[2]), school_minutes=parse_duration(a[3]), exercise_count=int(a[4]),
                    physics_q=int(a[5]), chemistry_q=int(a[6]), biology_q=int(a[7]), exercise_names="", notes="Entered by admin")
        rep = reports.submit_report(conn, part, session, reports.parse_date(a[1]), data, update.effective_user.id, by_admin=True)
    except (IndexError, ValueError, TypeError) as e:
        if isinstance(e, ValidationError):
            return await tg.reply(update, f"⚠️ {esc(e)}")
        return await tg.reply(update, "Use: /logreport &lt;id&gt; &lt;YYYY-MM-DD&gt; &lt;independent&gt; &lt;school&gt; &lt;exercises&gt; &lt;phy&gt; &lt;chem&gt; &lt;bio&gt;\n"
                                      "Example: /logreport 3 2026-11-05 8h 0 2 15 15 15")
    await tg.reply(update, f"✅ Report #{rep['id']} saved and approved.\n{esc(reports.provisional_points(rep, session).breakdown())}")


@guard
async def penalty_cmd(update, context):
    conn, session = _ctx(context)
    try:
        pid, kind = int(context.args[0]), context.args[1].lower()
        rest = context.args[2:]
        day = config.today()
        if rest and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rest[0]):
            day, rest = reports.parse_date(rest[0]), rest[1:]
        part = ppl.get(conn, pid)
        assert part and kind in reports.PENALTY_KINDS
    except (IndexError, ValueError, AssertionError):
        return await tg.reply(update, "Use: /penalty &lt;id&gt; wasted|partial|health [YYYY-MM-DD] [reason]")
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await confirm_prompt(update, context, "apply_penalty", {"pid": pid, "kind": kind, "date": day.isoformat(), "session_id": session["id"],
                                                            "reason": " ".join(rest) or None},
                         f"Apply <b>−{reports.penalty_amount(session, kind)}</b> ({kind}) to <b>{esc(part['display_name'])}</b> for {day}?")


@guard
async def status_cmd(update, context):
    conn, session = _ctx(context)
    try:
        pid, day, st = int(context.args[0]), reports.parse_date(context.args[1]), context.args[2].upper()
        reason = " ".join(context.args[3:])
        assert ppl.get(conn, pid) and (st in scoring.OVERRIDE_STATUSES or st == "CLEAR") and reason
    except (IndexError, ValueError, AssertionError):
        return await tg.reply(update, "Use: /status &lt;id&gt; &lt;YYYY-MM-DD&gt; GREEN|HALF|RED|HEALTH|EMPTY|CLEAR &lt;reason&gt;")
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await confirm_prompt(update, context, "set_status", {"pid": pid, "date": day.isoformat(), "status": st, "reason": reason,
                                                         "session_id": session["id"]}, f"Set <b>{st}</b> for participant {pid} on {day}?\nReason: {esc(reason)}")


@guard
async def newsession_cmd(update, context):
    conn, session = _ctx(context)
    try:
        a = context.args
        number, start, end = int(a[0]), reports.parse_date(a[1]), reports.parse_date(a[2])
        assert start <= end
    except (IndexError, ValueError, AssertionError):
        return await tg.reply(update, "Use: /newsession &lt;number&gt; &lt;start YYYY-MM-DD&gt; &lt;end YYYY-MM-DD&gt; [copy]\n"
                                      "'copy' also enrolls the current members. Rules are copied from the current session.")
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    name, group = (session["name"], session["group_name"]) if session else ("WINTER ARC", "THE OG ASSOCIATION")
    payload = {"name": name, "number": number, "group": group, "start": start.isoformat(), "end": end.isoformat(),
               "copy_from": session["id"] if session else None, "copy_members": "copy" in [x.lower() for x in a[3:]]}
    await confirm_prompt(update, context, "new_session", payload,
                         f"Create <b>{esc(name)} · Session {number:02d}</b> ({start} → {end}) and make it active?\n"
                         "The current session stays in the database untouched.")


@guard
async def sessions_cmd(update, context):
    await h_sess(update, context, [])


@guard
async def activate_cmd(update, context):
    conn, _ = _ctx(context)
    try:
        await h_ac(update, context, [context.args[0]])
    except (IndexError, ValueError):
        await tg.reply(update, "Use: /activate &lt;session id&gt;")


@guard
async def backup_cmd(update, context):
    conn, _ = _ctx(context)
    cfg = tg.cfg_of(context)
    if context.args and context.args[0].lower() == "group":  # test the off-site setup: send a backup to the backup group right now
        if not cfg.backup_chat_id:
            return await tg.reply(update, "BACKUP_CHAT_ID is not set. See BACKUP-RESTORE.md (create a private group, add the bot, send /chatid there).")
        res = await scheduler.run_offsite_backup(conn, context.bot, cfg, force=True)
        msg = {"sent": "✅ Backup sent to the backup group.", "failed": "❌ The backup could not be delivered. Is the bot a member of the group, and is BACKUP_CHAT_ID correct?",
               "empty-skipped": "ℹ️ Nothing to back up yet (no participants or reports)."}.get(res["status"], res["status"])
        return await tg.reply(update, msg)
    path = db.backup_database(conn, tg.cfg_of(context).backup_dir, "manual")
    with open(path, "rb") as fh:
        await context.bot.send_document(update.effective_chat.id, fh, filename=path.name, caption="🗄 Database backup (keep it private).")
    with db.transaction(conn):
        db.audit(conn, update.effective_user.id, "backup", "database", path.name)


async def chatid_cmd(update, context):
    """Admins only, works in groups: shows the chat's numeric ID (needed for BACKUP_CHAT_ID). Silent for everybody else."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or not tg.is_admin(context, user.id):
        return
    await tg.reply(update, f"Chat ID: <code>{chat.id}</code>\nType: {esc(chat.type)}\nFor off-site backups set <code>BACKUP_CHAT_ID</code> to this number.")


@guard
async def audit_cmd(update, context):
    conn, _ = _ctx(context)
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 15").fetchall()
    await tg.reply(update, "🗒 <b>Latest audit entries</b>\n" + "\n".join(
        f"{r['created_at'][5:16].replace('T', ' ')} · {r['actor_id']} · {esc(r['action'])} {esc(r['entity'] or '')} {esc(r['entity_id'] or '')}" for r in rows))


async def myid_cmd(update, context):
    """Public: anyone can look up their own numeric ID (needed for ADMIN_IDS and /link)."""
    await tg.reply(update, f"Your Telegram user ID: <code>{update.effective_user.id}</code>")


def build_handlers():
    """Returns (group0_handlers, group1_handlers). Group 1 only reads typed answers to admin prompts."""
    cmds = {"admin": guard(admin_cmd), "addp": addp_cmd, "import": import_cmd, "editp": editp_cmd, "link": link_cmd,
            "logreport": logreport_cmd, "penalty": penalty_cmd, "status": status_cmd, "newsession": newsession_cmd,
            "sessions": sessions_cmd, "activate": activate_cmd, "backup": backup_cmd, "audit": audit_cmd, "export": export_cmd}
    g0 = [CommandHandler(n, f) for n, f in cmds.items()]
    g0 += [CommandHandler("chart", chart_cmd), CommandHandler("dashboard", dashboard_cmd), CommandHandler("myid", myid_cmd),
           CommandHandler("chatid", chatid_cmd),
           CommandHandler("leaderboard", leaderboard.leaderboard_command), CommandHandler("board", leaderboard.leaderboard_command),
           CommandHandler("cancel", cancel_cmd),
           CallbackQueryHandler(leaderboard.leaderboard_callback, pattern=r"^lb:"),
           CallbackQueryHandler(admin_callback, pattern=r"^a:"), CallbackQueryHandler(confirm_callback, pattern=r"^cf:(y|n):")]
    g1 = [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, admin_text)]
    return g0, g1
