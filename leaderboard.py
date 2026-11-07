"""leaderboard.py - ranking logic (shared with chart/export) and the Telegram leaderboard UI."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

import config
import database as db
import reports
import tg
from utils import esc, fmt_hours

log = logging.getLogger(__name__)
PAGE_SIZE = 10

BOARDS = {  # kind -> (button label, title)
    "net": ("Points", "Net points"),
    "hours": ("Hours", "Study hours"),
    "streak": ("Streak", "Longest streak"),
    "days": ("Days", "Successful days"),
    "avg": ("Average", "Average study per day"),
}


def _tail(s):
    return (s.name.casefold(), s.participant_id)


def sort_key(kind):
    """Deterministic ordering: primary metric, then fixed tie-breakers, then name, then id."""
    return {
        "net": lambda s: (-s.net_points, -s.verified_minutes, -s.longest_streak, *_tail(s)),
        "hours": lambda s: (-s.verified_minutes, -s.net_points, -s.longest_streak, *_tail(s)),
        "streak": lambda s: (-s.longest_streak, -s.current_streak, -s.net_points, *_tail(s)),
        "days": lambda s: (-s.success_days, -s.net_points, -s.verified_minutes, *_tail(s)),
        "avg": lambda s: (-round(s.avg_minutes, 6), -s.net_points, -s.verified_minutes, *_tail(s)),
    }[kind]


def primary(kind, s):
    return {"net": s.net_points, "hours": s.verified_minutes, "streak": s.longest_streak,
            "days": s.success_days, "avg": round(s.avg_minutes, 6)}[kind]


def rank(summaries, kind="net"):
    """Returns [(rank, summary)] - equal primary values share a rank."""
    ordered = sorted((s for s in summaries if s.active), key=sort_key(kind))
    out, last_val, last_rank = [], None, 0
    for i, s in enumerate(ordered, 1):
        v = primary(kind, s)
        last_rank = last_rank if v == last_val else i
        last_val = v
        out.append((last_rank, s))
    return out


def standings(conn, session, today=None, kind="net"):
    return rank(reports.build_summaries(conn, session, today), kind)


def _line(kind, r, s):
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r, f"{r}.")
    name = esc(s.name)
    detail = {
        "net": f"<b>{s.net_points}</b> pts ({s.gross_points} − {s.penalty_total}) · {fmt_hours(s.verified_minutes)} · 🔥{s.current_streak}",
        "hours": f"<b>{fmt_hours(s.verified_minutes)}</b> · {s.net_points} pts",
        "streak": f"<b>{s.longest_streak}</b>-day best · current {s.current_streak}",
        "days": f"<b>{s.success_days}</b> full-green · {s.partial_days} partial",
        "avg": f"<b>{fmt_hours(s.avg_minutes)}</b>/day · {fmt_hours(s.verified_minutes)} total",
    }[kind]
    return f"{medal} {name} — {detail}"


def render(session, ranked, kind="net", page=0):
    pages = max(1, -(-len(ranked) // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = ranked[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    head = (f"🏆 <b>{esc(reports.session_title(session)).upper()}</b>\n"
            f"{esc(session['group_name'])} · {reports.date_range_label(session)}\n"
            f"<b>{BOARDS[kind][1]}</b> · page {page + 1}/{pages}"
            + ("" if not session["archived"] else " · <i>archived session</i>") + "\n")
    body = "\n".join(_line(kind, r, s) for r, s in chunk) or "No active participants yet."
    foot = "\n\n<i>Only admin-approved reports count.</i>"
    return head + "\n" + body + foot, page, pages


def keyboard(kind, page, pages):
    kinds = [(("• " if k == kind else "") + BOARDS[k][0], f"lb:{k}:0") for k in BOARDS]
    nav = []
    if page > 0:
        nav.append(("⬅️ Prev", f"lb:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Next ➡️", f"lb:{kind}:{page + 1}"))
    return tg.kb([kinds[:3], kinds[3:], nav, [("👤 My profile", "u:profile"), ("🏠 Menu", "u:menu")]])


async def _show(update, context, kind, page, edit):
    conn = tg.conn_of(context)
    session = db.get_active_session(conn)
    if session is None:
        text, markup = "No active challenge session right now.", None
    else:
        ranked = standings(conn, session, config.today(), kind)
        text, page, pages = render(session, ranked, kind, page)
        markup = keyboard(kind, page, pages)
    if edit:
        await tg.safe_edit(update.callback_query, text, markup)
    else:
        await tg.reply(update, text, markup)


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tg.limiter.allow(("lb", update.effective_user.id), 2):
        return
    kind = (context.args[0].lower() if context.args else "net")
    await _show(update, context, kind if kind in BOARDS else "net", 0, edit=False)


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await tg.safe_answer(q)
    try:
        _, kind, page = q.data.split(":")
        kind, page = (kind if kind in BOARDS else "net"), int(page)
    except ValueError:
        return
    await _show(update, context, kind, page, edit=True)
