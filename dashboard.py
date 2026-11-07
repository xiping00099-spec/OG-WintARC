"""dashboard.py - the separate statistics dashboard image (all numbers come from real records)."""
from PIL import Image, ImageDraw

import imaging as im
import leaderboard
import reports
from utils import fmt_hours


def dashboard_data(session, summaries, today):
    """Plain dict of totals for the active participants. Used by the image AND the text dashboard."""
    act = [s for s in summaries if s.active]
    best = max(act, key=lambda s: (s.current_streak, -s.participant_id), default=None)
    start, end = reports.session_range(session)
    last = min(today, end)
    return {
        "active_participants": len(act),
        "approved_reports": sum(s.approved_reports for s in act),
        "pending_reports": sum(s.pending_reports for s in act),
        "verified_minutes": sum(s.verified_minutes for s in act),
        "gross_points": sum(s.gross_points for s in act),
        "penalty_total": sum(s.penalty_total for s in act),
        "net_points": sum(s.net_points for s in act),
        "success_days": sum(s.success_days for s in act),
        "partial_days": sum(s.partial_days for s in act),
        "longest_current_streak": best.current_streak if best else 0,
        "streak_holder": best.name if best and best.current_streak > 0 else "-",
        "top5": [s for _, s in leaderboard.rank(summaries, "net")[:5]],
        "day_no": max(0, (last - start).days + 1) if today >= start else 0,
        "total_days": (end - start).days + 1,
    }


def dashboard_text(session, data) -> str:
    from utils import esc
    return (
        f"📊 <b>{esc(reports.session_title(session)).upper()}</b>\n{reports.date_range_label(session)}\n\n"
        f"Active participants: <b>{data['active_participants']}</b>\n"
        f"Approved reports: <b>{data['approved_reports']}</b>\n"
        f"Pending reports: <b>{data['pending_reports']}</b>\n"
        f"Verified study: <b>{fmt_hours(data['verified_minutes'])}</b>\n"
        f"Gross points: <b>{data['gross_points']}</b>\n"
        f"Penalty deductions: <b>{data['penalty_total']}</b>\n"
        f"Net points: <b>{data['net_points']}</b>\n"
        f"Full-green days: <b>{data['success_days']}</b> · partial: <b>{data['partial_days']}</b>\n"
        f"Longest current streak: <b>{data['longest_current_streak']}</b> ({esc(data['streak_holder'])})")


def render_dashboard(session, data, generated_at, scale=2):
    k = scale
    S = lambda v: int(round(v * k))  # noqa: E731
    W, margin = 1250, 36
    card_w, card_h, gap = 218, 104, 16
    H = 150 + 2 * (card_h + gap) + 40 + 56 + 5 * 44 + 80
    img = Image.new("RGB", (S(W), S(H)), im.BG)
    d = ImageDraw.Draw(img)
    d.text((S(margin), S(24)), "WINTER ARC", font=im.font(S(42), True), fill=im.WHITE)
    d.text((S(margin), S(76)), f"SESSION {int(session['session_number']):02d}  ·  STATISTICS DASHBOARD",
           font=im.font(S(20), True), fill=im.LIME)
    d.text((S(margin), S(108)), session["group_name"].upper(), font=im.font(S(16)), fill=im.WHITE)
    f = im.font(S(16), True)
    period = f"PERIOD {reports.date_range_label(session)}"
    d.text((S(W - margin) - im.text_w(d, period, f), S(34)), period, font=f, fill=im.WHITE)
    note = f"Day {data['day_no']} of {data['total_days']} · verified records only"
    f2 = im.font(S(14))
    d.text((S(W - margin) - im.text_w(d, note, f2), S(64)), note, font=f2, fill=im.MUTED)

    cards = [
        ("ACTIVE PARTICIPANTS", str(data["active_participants"]), im.WHITE),
        ("APPROVED REPORTS", str(data["approved_reports"]), im.LIME),
        ("PENDING REPORTS", str(data["pending_reports"]), im.AMBER),
        ("VERIFIED STUDY HOURS", f"{data['verified_minutes'] / 60:.1f}", im.WHITE),
        ("GROSS POINTS", str(data["gross_points"]), im.WHITE),
        ("PENALTY DEDUCTIONS", f"-{data['penalty_total']}" if data["penalty_total"] else "0", im.RED),
        ("NET POINTS", str(data["net_points"]), im.LIME),
        ("FULL-GREEN DAYS", str(data["success_days"]), im.LIME),
        ("PARTIAL DAYS", str(data["partial_days"]), im.WHITE),
        ("LONGEST CURRENT STREAK", str(data["longest_current_streak"]), im.WHITE),
    ]
    f_l, f_v, f_s = im.font(S(12), True), im.font(S(36), True), im.font(S(13))
    y_cards = 150
    for i, (label, value, color) in enumerate(cards):
        col, row = i % 5, i // 5
        x, y = margin + col * (card_w + gap), y_cards + row * (card_h + gap)
        d.rounded_rectangle([S(x), S(y), S(x + card_w), S(y + card_h)], S(10), fill=im.PANEL, outline=im.GRID, width=S(1))
        d.text((S(x + 16), S(y + 16)), label, font=f_l, fill=im.MUTED)
        d.text((S(x + 16), S(y + 42)), value, font=f_v, fill=color)
        if label.startswith("LONGEST"):
            d.text((S(x + 16), S(y + card_h - 22)), im.fit(d, data["streak_holder"], f_s, S(card_w - 32)), font=f_s, fill=im.MUTED)
    # ---- top five
    ty = y_cards + 2 * (card_h + gap) + 24
    d.text((S(margin), S(ty)), "TOP FIVE BY NET POINTS", font=im.font(S(18), True), fill=im.WHITE)
    ty += 40
    cols = [("#", margin, "lm"), ("PARTICIPANT", margin + 50, "lm"), ("HOURS", 700, "mm"), ("GROSS", 810, "mm"),
            ("PENALTY", 920, "mm"), ("NET", 1030, "mm"), ("STREAK", 1140, "mm")]
    for label, x, anchor in cols:
        d.text((S(x), S(ty + 8)), label, font=f_l, fill=im.MUTED, anchor=anchor)
    d.line([(S(margin), S(ty + 24)), (S(W - margin), S(ty + 24))], fill=im.GRID, width=S(1))
    f_n, f_r = im.font(S(17), True), im.font(S(17))
    top = data["top5"]
    for i in range(5):
        y = ty + 24 + i * 44 + 22
        if i < len(top):
            s = top[i]
            vals = [str(i + 1), im.fit(d, s.name, f_n, S(560)), f"{s.verified_minutes / 60:.1f}", str(s.gross_points),
                    f"-{s.penalty_total}" if s.penalty_total else "0", str(s.net_points), str(s.current_streak)]
        else:
            vals = [str(i + 1), "—", "", "", "", "", ""]
        for (label, x, anchor), v in zip(cols, vals):
            color = im.LIME if label == "NET" else (im.RED if label == "PENALTY" and v not in ("0", "") else im.WHITE)
            d.text((S(x), S(y)), v, font=f_n if label == "PARTICIPANT" else f_r, fill=color, anchor=anchor)
    d.text((S(margin), S(H - 26)), f"Generated {generated_at.strftime('%d %b %Y, %H:%M')} IST  ·  {reports.session_title(session)}",
           font=im.font(S(12)), fill=im.MUTED, anchor="lm")
    return im.to_png(img)
