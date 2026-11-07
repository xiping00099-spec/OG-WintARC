"""participant.py - everything a normal participant sees: menu, registration, targets, reports, profile."""
import logging
from datetime import timedelta

from telegram import Update
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler,
                          filters)

import config
import database as db
import dbconn
import participants as ppl
import reports
import scoring
import tg
from utils import ValidationError, esc, fmt_minutes, parse_count, parse_duration, clean_text, fmt_hours

log = logging.getLogger(__name__)

PICK_DATE, STEPS, CONFIRM = range(3)
MENU_TEXT = "🏠 <b>Main menu</b>\nWhat would you like to do?"


def menu_keyboard():
    return tg.kb([
        [("🎯 Submit daily targets", "u:target"), ("📝 Submit daily report", "u:report")],
        [("👤 My profile", "u:profile"), ("🧮 My daily points", "u:points")],
        [("📅 My history", "u:history"), ("🏆 Leaderboard", "lb:net:0")],
        [("📜 Challenge rules", "u:rules")],
    ])


# ----------------------------------------------------------------- shared lookups
def _context(update, context):
    """Returns (conn, session, participant_row_or_None) and keeps the stored username fresh."""
    conn = tg.conn_of(context)
    session = db.get_active_session(conn)
    me = ppl.refresh_username(conn, update.effective_user)
    return conn, session, me


async def _answer(update):
    if update.callback_query:
        await tg.safe_answer(update.callback_query)


def _need_registration_text():
    return "You are not registered yet. Send /register to join the challenge."


# ----------------------------------------------------------------- start / menu / register
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn, session, me = _context(update, context)
    if me:  # they can obviously be messaged privately now
        conn.execute("DELETE FROM reminders_sent WHERE participant_id=? AND status='blocked'", (me["id"],))
    title = f"<b>{esc(reports.session_title(session)).upper()}</b>\n" if session else ""
    if me is None:
        await tg.reply(update, f"👋 Welcome to {title}\nYou are not registered yet.",
                       tg.kb([[("✅ Register now", "reg:start")]]))
        return
    await tg.reply(update, f"👋 Hi {esc(me['display_name'])}!\n\n" + MENU_TEXT, menu_keyboard())


async def menu(update, context):
    await _answer(update)
    await tg.reply(update, MENU_TEXT, menu_keyboard())


async def register_cmd(update, context):
    await _answer(update)
    conn, session, me = _context(update, context)
    if session is None:
        return await tg.reply(update, "There is no active challenge session right now.")
    if me and ppl.is_member(conn, session["id"], me["id"]):
        return await tg.reply(update, "✅ You are already registered for this session.", menu_keyboard())
    await tg.reply(update, "How do you study during this challenge?",
                   tg.kb([[("🏫 School-going", "reg:school"), ("🏠 Dummy-school", "reg:dummy")]]))


async def register_choice(update, context):
    q = update.callback_query
    await tg.safe_answer(q)
    kind = q.data.split(":")[1]
    if kind == "start":
        return await register_cmd(update, context)
    conn, session, me = _context(update, context)
    if session is None:
        return await tg.safe_edit(q, "There is no active challenge session right now.")
    if not tg.limiter.allow(("reg", update.effective_user.id), 3):
        return
    user = update.effective_user
    name = (user.full_name or user.first_name or user.username or f"User {user.id}").strip()
    try:
        row = ppl.register(conn, name, kind, session["id"], telegram_user_id=user.id, username=user.username)
    except ValidationError as e:
        return await tg.safe_edit(q, f"⚠️ {esc(e)}")
    await tg.safe_edit(q, f"✅ Registered as <b>{esc(row['display_name'])}</b> "
                          f"({ppl.SCHOOL_LABELS[row['school_type']]}).\nUse /rename to change your display name.",
                       menu_keyboard())


async def rename_cmd(update, context):
    conn, session, me = _context(update, context)
    if not me:
        return await tg.reply(update, _need_registration_text())
    name = " ".join(context.args).strip()
    if not name:
        return await tg.reply(update, "Use: /rename Your Name")
    try:
        ppl.update(conn, me["id"], update.effective_user.id, display_name=name)
    except ValidationError as e:
        return await tg.reply(update, f"⚠️ {esc(e)}")
    await tg.reply(update, f"✅ Your display name is now <b>{esc(name)}</b>.")


# ----------------------------------------------------------------- read-only views
def _status_emoji(st):
    return {"GREEN": "🟩", "HALF": "🟨", "RED": "🟥", "PENDING": "🟧", "HEALTH": "🟦", "EMPTY": "⬜"}[st]


async def profile(update, context):
    await _answer(update)
    conn, session, me = _context(update, context)
    if not me:
        return await tg.reply(update, _need_registration_text())
    if session is None:
        return await tg.reply(update, "No active session.")
    s = reports.get_summary(conn, session, me["id"], config.today())
    if s is None:
        return await tg.reply(update, "You are not enrolled in the current session. Use /register.")
    text = (f"👤 <b>{esc(me['display_name'])}</b> ({ppl.SCHOOL_LABELS[me['school_type']]})\n"
            f"{esc(reports.session_title(session))} · {reports.date_range_label(session)}\n\n"
            f"Net points: <b>{s.net_points}</b>  (gross {s.gross_points} − penalties {s.penalty_total})\n"
            f"Verified study: <b>{fmt_minutes(s.verified_minutes)}</b> · avg {fmt_hours(s.avg_minutes)}/day\n"
            f"Streak: current <b>{s.current_streak}</b> · longest <b>{s.longest_streak}</b>\n"
            f"Days: 🟩 {s.success_days} full · 🟨 {s.partial_days} partial · 🟥 {s.unsuccessful_days} failed · "
            f"🟦 {s.health_days} health\n"
            f"Reports: ✅ {s.approved_reports} approved · ⏳ {s.pending_reports} pending · "
            f"✏️ {s.correction_requests} need correction · ❓ {s.missing_reports} missing\n"
            f"Questions: Physics {s.physics_total} · Chemistry {s.chemistry_total} · Biology {s.biology_total}\n"
            f"Exercise: {s.exercise_total} sessions on {s.exercise_days} days")
    await tg.reply(update, text, tg.kb([[("🧮 Daily points", "u:points"), ("📅 History", "u:history")],
                                        [("🏠 Menu", "u:menu")]]))


async def points(update, context):
    await _answer(update)
    conn, session, me = _context(update, context)
    if not me or session is None:
        return await tg.reply(update, _need_registration_text())
    s = reports.get_summary(conn, session, me["id"], config.today())
    if s is None:
        return await tg.reply(update, "You are not enrolled in the current session.")
    rows = [(d, r) for d, r in sorted(s.days.items(), reverse=True) if r.points is not None or r.penalty]
    if not rows:
        return await tg.reply(update, "No approved days yet. Points appear after an admin approves your report.")
    out = ["🧮 <b>Your verified daily points</b> (latest first)\n"]
    for d, r in rows[:10]:
        p = r.points
        if p:
            out.append(f"<b>{d.strftime('%d %b')}</b>  {fmt_minutes(p.raw_minutes)} → study {p.study_points} + "
                       f"exercise {p.exercise_points} + subjects {p.subject_points} = <b>{p.gross}</b>"
                       + (f"  −{r.penalty} penalty" if r.penalty else ""))
        else:
            out.append(f"<b>{d.strftime('%d %b')}</b>  penalty −{r.penalty}")
    out.append(f"\nTotal: gross {s.gross_points} − penalties {s.penalty_total} = <b>{s.net_points}</b> net")
    await tg.reply(update, "\n".join(out), tg.kb([[("🏠 Menu", "u:menu")]]))


async def history(update, context):
    await _answer(update)
    conn, session, me = _context(update, context)
    if not me or session is None:
        return await tg.reply(update, _need_registration_text())
    s = reports.get_summary(conn, session, me["id"], config.today())
    if s is None:
        return await tg.reply(update, "You are not enrolled in the current session.")
    start, end = reports.session_range(session)
    last = min(end, config.today())
    cells = [_status_emoji(s.days[d].status) for d in scoring.daterange(start, last)]
    rows = ["".join(cells[i:i + 10]) for i in range(0, len(cells), 10)]
    text = ("📅 <b>Your history</b>\n" + "\n".join(f"Day {i * 10 + 1}-{min((i + 1) * 10, len(cells))}: {r}"
                                                  for i, r in enumerate(rows)) +
            "\n\n🟩 full  🟨 partial  🟥 failed  🟧 pending  🟦 health  ⬜ no record")
    await tg.reply(update, text, tg.kb([[("🏠 Menu", "u:menu")]]))


async def rules(update, context):
    await _answer(update)
    conn, session, _ = _context(update, context)
    if session is None:
        return await tg.reply(update, "No active session.")
    r = reports.rules_of(session)
    policy = ("Hours count in full; a leftover within "
              f"{r.tolerance_minutes} min of the next hour counts as that hour."
              if r.rounding_policy == scoring.ROUND_TOLERANCE else "Only complete hours count (no tolerance).")
    text = (f"📜 <b>{esc(reports.session_title(session)).upper()} — Rules</b>\n\n"
            f"<b>Points per day</b>\n• {r.points_per_hour} point per study hour. {policy}\n"
            f"• Exercise: 1 point per session, max {r.max_exercise_points}/day\n"
            f"• {r.subject_point_value} point each for ≥{r.subject_min_questions} Physics / Chemistry / Biology questions\n\n"
            f"<b>Full green</b>: {r.full_green_minutes // 60}h"
            f"{' (with ' + str(r.tolerance_minutes) + ' min tolerance)' if r.tolerance_minutes and r.rounding_policy == scoring.ROUND_TOLERANCE else ''}\n\n"
            f"<b>Penalties</b>\n• Wasted day: −{r.penalty_wasted}\n"
            f"• Partial day (below threshold): " + (f"−{r.penalty_partial}" if r.apply_partial_penalty else "off") + "\n"
            f"• Health day: " + (f"−{r.penalty_health}" if r.health_penalty_confirmed else "recorded separately, no deduction") +
            f"\n\n<b>Targets</b> are due by {session['target_deadline']} IST on the day they apply to.\n"
            "<b>Only admin-approved reports</b> count towards points and the leaderboard.")
    await tg.reply(update, text, tg.kb([[("🏠 Menu", "u:menu")]]))


# ----------------------------------------------------------------- step engine (targets + reports)
def _dur_parser(label):
    return lambda t, d: parse_duration(t)


def _count(label, maximum):
    return lambda t, d: parse_count(t, maximum, label)


def _text_opt(label, n):
    def parse(t, d):
        return "" if t.strip().lower() in ("-", "skip", "none", "no") else clean_text(t, n, label)
    return parse


REPORT_STEPS = [
    ("independent_minutes", "⏱ <b>Independent study time</b> today?\nExamples: 6:30, 6h30m, 6.5h, 390m (or 0)", _dur_parser("")),
    ("school_minutes", "🏫 <b>Actual school study time</b> today? (e.g. 2h, 90m or 0)", _dur_parser(""),
     lambda d: d["school_type"] != "school"),
    ("exercise_count", f"💪 How many <b>exercise sessions</b> did you do? (0-{scoring.MAX_EXERCISE_COUNT})",
     _count("Exercise count", scoring.MAX_EXERCISE_COUNT)),
    ("exercise_names", "💪 Which exercises? (e.g. pushups, running) or - to skip", _text_opt("Exercise names", 200),
     lambda d: d.get("exercise_count", 0) == 0),
    ("physics_q", "⚛️ <b>Physics</b> questions completed? (number)", _count("Physics questions", scoring.MAX_QUESTIONS)),
    ("chemistry_q", "🧪 <b>Chemistry</b> questions completed? (number)", _count("Chemistry questions", scoring.MAX_QUESTIONS)),
    ("biology_q", "🧬 <b>Biology</b> questions completed? (number)", _count("Biology questions", scoring.MAX_QUESTIONS)),
    ("notes", "📝 Any notes for the admins? (or - to skip)", _text_opt("Notes", 500)),
]
TARGET_STEPS = [
    ("planned_minutes", "⏱ <b>Planned study time</b> for the day? (e.g. 8h, 7:30 or 0)", _dur_parser("")),
    ("planned_physics", "⚛️ Planned <b>Physics</b> questions?", _count("Planned Physics questions", scoring.MAX_QUESTIONS)),
    ("planned_chemistry", "🧪 Planned <b>Chemistry</b> questions?", _count("Planned Chemistry questions", scoring.MAX_QUESTIONS)),
    ("planned_biology", "🧬 Planned <b>Biology</b> questions?", _count("Planned Biology questions", scoring.MAX_QUESTIONS)),
    ("planned_exercises", f"💪 Planned <b>exercise sessions</b>? (0-{scoring.MAX_EXERCISE_COUNT})",
     _count("Planned exercises", scoring.MAX_EXERCISE_COUNT)),
    ("notes", "📝 Optional notes? (or - to skip)", _text_opt("Notes", 500)),
]
FLOWS = {"report": REPORT_STEPS, "target": TARGET_STEPS}
CANCEL_KB = lambda: tg.kb([[("❌ Cancel", "flow:cancel")]])  # noqa: E731


def _next_index(flow, data, i):
    steps = FLOWS[flow]
    while i < len(steps) and len(steps[i]) > 3 and steps[i][3](data):
        data.setdefault(steps[i][0], 0 if steps[i][0] != "exercise_names" else "")
        i += 1
    return i


async def _ask(update, context):
    ud = context.user_data["flow"]
    key, prompt = FLOWS[ud["kind"]][ud["i"]][:2]
    await tg.reply(update, f"{prompt}\n<i>Step {ud['i'] + 1}/{len(FLOWS[ud['kind']])} · /cancel to stop</i>", CANCEL_KB())


async def _begin(update, context, kind):
    await _answer(update)
    if update.effective_chat.type != "private":
        name = context.bot.username
        await tg.reply(update, "🔒 Please use targets and reports in a private chat with me."
                       + (f"\n👉 https://t.me/{name}?start=menu" if name else ""))
        return ConversationHandler.END
    conn, session, me = _context(update, context)
    if not me:
        await tg.reply(update, _need_registration_text())
        return ConversationHandler.END
    if session is None or not ppl.is_member(conn, session["id"], me["id"]):
        await tg.reply(update, "You are not enrolled in the current session. Send /register.")
        return ConversationHandler.END
    today = config.today()
    start, end = reports.session_range(session)
    if kind == "report":
        days = [today - timedelta(days=i) for i in range(session["report_window_days"])]
        word = "report"
    else:
        days = [today, today + timedelta(days=1)]
        word = "targets"
    days = [d for d in days if start <= d <= end]
    if not days:
        await tg.reply(update, "The session isn't running on those dates.")
        return ConversationHandler.END
    context.user_data["flow"] = {"kind": kind, "data": {"school_type": me["school_type"]}, "i": 0}
    buttons = [(d.strftime("%a %d %b") + (" (today)" if d == today else ""), f"fd:{d.isoformat()}") for d in days]
    await tg.reply(update, f"Which date is this {word} for?", tg.kb([buttons, [("❌ Cancel", "flow:cancel")]]))
    return PICK_DATE


async def begin_report(update, context):
    return await _begin(update, context, "report")


async def begin_target(update, context):
    return await _begin(update, context, "target")


async def pick_date(update, context):
    q = update.callback_query
    await tg.safe_answer(q)
    ud = context.user_data.get("flow")
    if not ud:
        return ConversationHandler.END
    try:
        ud["date"] = reports.parse_date(q.data.split(":", 1)[1])
    except ValidationError:
        return PICK_DATE
    ud["i"] = _next_index(ud["kind"], ud["data"], 0)
    await tg.safe_edit(q, f"📅 Date: <b>{ud['date'].strftime('%A %d %b %Y')}</b>")
    await _ask(update, context)
    return STEPS


async def on_step(update, context):
    ud = context.user_data.get("flow")
    if not ud:
        return ConversationHandler.END
    steps = FLOWS[ud["kind"]]
    key, _, parser = steps[ud["i"]][:3]
    try:
        value = parser(update.effective_message.text or "", ud["data"])
    except ValidationError as e:
        await tg.reply(update, f"⚠️ {esc(e)}\nPlease try again.", CANCEL_KB())
        return STEPS
    ud["data"][key] = value
    ud["i"] = _next_index(ud["kind"], ud["data"], ud["i"] + 1)
    if ud["i"] < len(steps):
        await _ask(update, context)
        return STEPS
    await tg.reply(update, _summary(ud, provisional=False), tg.kb([[("✅ Submit", "flow:ok"), ("❌ Cancel", "flow:cancel")]]))
    return CONFIRM


def _summary(ud, provisional=True):
    d, day = ud["data"], ud["date"].strftime("%a %d %b %Y")
    if ud["kind"] == "target":
        return (f"🎯 <b>Targets for {day}</b>\nStudy {fmt_minutes(d['planned_minutes'])} · Physics {d['planned_physics']} · "
                f"Chemistry {d['planned_chemistry']} · Biology {d['planned_biology']} · Exercise {d['planned_exercises']}"
                + (f"\nNotes: {esc(d['notes'])}" if d.get("notes") else "") + "\n\nSubmit?")
    return (f"📝 <b>Report for {day}</b>\nIndependent: {fmt_minutes(d['independent_minutes'])}"
            + (f" · School: {fmt_minutes(d['school_minutes'])}" if d["school_type"] == "school" else "") +
            f"\nExercise: {d['exercise_count']}" + (f" ({esc(d['exercise_names'])})" if d.get("exercise_names") else "") +
            f"\nPhysics {d['physics_q']} · Chemistry {d['chemistry_q']} · Biology {d['biology_q']}"
            + (f"\nNotes: {esc(d['notes'])}" if d.get("notes") else "") + "\n\nSubmit?")


def _saved_report_matches(conn, me, session, ud, data):
    """After a database failure: True = the report IS stored exactly as entered, False = it is not, None = cannot tell."""
    try:
        clean = reports.validate_report_fields(data, me)
        row = reports.get_report(conn, me["id"], session["id"], ud["date"])
    except (dbconn.DatabaseUnavailable, ValidationError):
        return None
    if row is None:
        return False
    same = all(row[k] == clean[k] for k in ("independent_minutes", "school_minutes", "exercise_count", "exercise_names", "physics_q",
                                            "chemistry_q", "biology_q", "notes"))
    return same and row["status"] in ("pending", "approved")


def _saved_target_matches(conn, me, session, ud, data):
    try:
        clean = reports.validate_target_fields(data)
        row = reports.get_target(conn, me["id"], session["id"], ud["date"])
    except (dbconn.DatabaseUnavailable, ValidationError):
        return None
    if row is None:
        return False
    return all(row[k] == clean[k] for k in ("planned_minutes", "planned_physics", "planned_chemistry", "planned_biology", "planned_exercises", "notes"))


def _outage_message(kind, day, verdict):
    what = "report" if kind == "report" else "targets"
    cmd = "/report" if kind == "report" else "/target"
    when = day.strftime("%d %b")
    if verdict is True:
        return (f"✅ <b>Saved.</b> The database was slow to answer, but your {what} for {when} <b>is stored</b>. "
                "You do not need to send it again.")
    if verdict is False:
        return (f"❌ <b>NOT saved.</b> The database was unavailable, so your {what} for {when} was <b>not stored</b>. "
                f"Press <b>Try again</b> (or send {cmd} later). Sending it again is safe — it never creates a duplicate.")
    return (f"⚠️ <b>Could not confirm.</b> The database stopped answering, so I can't tell whether your {what} for {when} was saved. "
            f"Open /history to check. If it is not there, press <b>Try again</b>. Sending it again is safe — it never creates a duplicate.")


async def confirm(update, context):
    q = update.callback_query
    await tg.safe_answer(q)
    ud = context.user_data.pop("flow", None)
    if not ud:
        await tg.safe_edit(q, "This form has expired. Start again from /menu.")
        return ConversationHandler.END
    if not tg.limiter.allow(("submit", update.effective_user.id), 2):  # double-tap protection: ignore the press but KEEP the form
        context.user_data["flow"] = ud
        return CONFIRM
    conn = tg.conn_of(context)
    session = me = None
    uid = update.effective_user.id
    data = {k: v for k, v in ud["data"].items() if k != "school_type"}
    try:
        _, session, me = _context(update, context)
        if session is None or me is None:
            raise ValidationError("You are not registered for an active session.")
        if ud["kind"] == "report":
            rep = reports.submit_report(conn, me, session, ud["date"], data, uid)  # identity re-checked server-side
            pts = reports.provisional_points(rep, session)
            await tg.safe_edit(q, (
                f"✅ <b>Report received</b> for {ud['date'].strftime('%d %b')}.\n\n{esc(pts.breakdown())}\n\n"
                "⏳ These points are <b>provisional</b> — the score stays pending until an admin approves your report."),
                tg.kb([[("🏠 Menu", "u:menu")]]))
        else:
            _, created = reports.submit_target(conn, me, session, ud["date"], data, uid)
            await tg.safe_edit(q, f"✅ Targets {'saved' if created else 'updated'} for {ud['date'].strftime('%d %b')}. "
                                  "You can edit them until the deadline.", tg.kb([[("🏠 Menu", "u:menu")]]))
    except ValidationError as e:
        await tg.safe_edit(q, f"⚠️ {esc(e)}", tg.kb([[("🏠 Menu", "u:menu")]]))
    except dbconn.DatabaseUnavailable as e:
        log.warning("Database unavailable while saving a %s: %s", ud["kind"], e)
        verdict = None
        if me is not None and session is not None:
            check = _saved_report_matches if ud["kind"] == "report" else _saved_target_matches
            verdict = check(conn, me, session, ud, data)  # asks the database again: was it saved after all?
        text = _outage_message(ud["kind"], ud["date"], verdict)
        if verdict is True:
            await tg.safe_edit(q, text, tg.kb([[("🏠 Menu", "u:menu")]]))
        else:
            context.user_data["flow"] = ud  # keep the answers so "Try again" needs no re-typing
            tg.limiter.reset(("submit", update.effective_user.id))  # a failed save must not block the immediate retry
            await tg.safe_edit(q, text, tg.kb([[("🔁 Try again", "flow:ok"), ("❌ Cancel", "flow:cancel")]]))
            return CONFIRM
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.pop("flow", None)
    if update.callback_query:
        await tg.safe_answer(update.callback_query, "Cancelled")
        await tg.safe_edit(update.callback_query, "❌ Cancelled.", tg.kb([[("🏠 Menu", "u:menu")]]))
    else:
        await tg.reply(update, "❌ Cancelled.", tg.kb([[("🏠 Menu", "u:menu")]]))
    return ConversationHandler.END


async def timed_out(update, context):
    context.user_data.pop("flow", None)
    try:
        await tg.reply(update, "⌛ That form timed out. Start again from /menu.")
    except Exception:  # noqa: BLE001 - the timeout notice is best effort
        pass
    return ConversationHandler.END


async def stale_button(update, context):
    """A form/button from an old message: tell the user instead of failing silently."""
    await tg.safe_answer(update.callback_query, "This button has expired. Open /menu again.", show_alert=True)


def build_handlers():
    private_text = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE
    conv = ConversationHandler(
        entry_points=[CommandHandler("report", begin_report), CommandHandler("target", begin_target),
                      CallbackQueryHandler(begin_report, pattern=r"^u:report$"),
                      CallbackQueryHandler(begin_target, pattern=r"^u:target$")],
        states={
            PICK_DATE: [CallbackQueryHandler(pick_date, pattern=r"^fd:\d{4}-\d{2}-\d{2}$")],
            STEPS: [MessageHandler(private_text, on_step)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^flow:ok$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timed_out), CallbackQueryHandler(timed_out)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
                   CommandHandler(["start", "menu"], cancel)],
        conversation_timeout=900, allow_reentry=True, name="forms", persistent=False)
    return [
        conv,
        CommandHandler("start", start), CommandHandler("menu", menu), CommandHandler("register", register_cmd),
        CommandHandler("rename", rename_cmd), CommandHandler("profile", profile), CommandHandler("points", points),
        CommandHandler("history", history), CommandHandler("rules", rules),
        CallbackQueryHandler(menu, pattern=r"^u:menu$"), CallbackQueryHandler(profile, pattern=r"^u:profile$"),
        CallbackQueryHandler(points, pattern=r"^u:points$"), CallbackQueryHandler(history, pattern=r"^u:history$"),
        CallbackQueryHandler(rules, pattern=r"^u:rules$"), CallbackQueryHandler(register_choice, pattern=r"^reg:"),
        CallbackQueryHandler(stale_button, pattern=r"^(fd|flow):"),
    ]
