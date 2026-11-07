"""chart.py - the black & lime WINTER ARC attendance chart, drawn directly with Pillow (no emojis)."""
import math

from PIL import Image, ImageDraw

import imaging as im
import leaderboard
import reports
import scoring

STAT_COLS = ["HOURS", "GROSS", "PENALTY", "NET", "STREAK"]


def render_attendance(session, summaries, generated_at, page_size=22, scale=2):
    """Returns a list of PNG byte strings (one per page of `page_size` participants)."""
    ordered = [s for _, s in leaderboard.rank(summaries, "net")]  # same order as the leaderboard
    page_size = max(1, int(page_size))
    pages = [ordered[i:i + page_size] for i in range(0, len(ordered), page_size)] or [[]]
    return [im.to_png(_render_page(session, chunk, i + 1, len(pages), generated_at, scale))
            for i, chunk in enumerate(pages)]


def _render_page(session, rows, page_no, page_count, generated_at, k):
    S = lambda v: int(round(v * k))  # noqa: E731  (scale helper)
    start, end = reports.session_range(session)
    days = list(scoring.daterange(start, end))
    n = len(days)
    cell = 28 if n <= 31 else max(14, int(900 / n))
    pitch = cell + 4
    name_w, stat_w, margin = 200, 78, 30
    head_h, th_h = 130, 58
    row_h = max(cell + 8, 34)
    legend_h, foot_h = 70, 40
    width = margin * 2 + name_w + n * pitch + len(STAT_COLS) * stat_w
    width = max(width, 900)
    height = head_h + th_h + max(1, len(rows)) * row_h + legend_h + foot_h

    img = Image.new("RGB", (S(width), S(height)), im.BG)
    d = ImageDraw.Draw(img)

    # ---- header
    d.text((S(margin), S(26)), "WINTER ARC" if session["name"].upper() == "WINTER ARC" else session["name"].upper(),
           font=im.font(S(46), True), fill=im.WHITE)
    d.text((S(margin), S(80)), f"SESSION {int(session['session_number']):02d}", font=im.font(S(22), True), fill=im.LIME)
    sw = im.text_w(d, f"SESSION {int(session['session_number']):02d}", im.font(S(22), True))
    d.text((S(margin) + sw + S(18), S(82)), session["group_name"].upper(), font=im.font(S(20)), fill=im.WHITE)
    right = S(width - margin)
    f_range = im.font(S(18), True)
    rng = reports.date_range_label(session)
    d.text((right - im.text_w(d, rng, f_range), S(34)), rng, font=f_range, fill=im.WHITE)
    sub = f"Verified records only · page {page_no}/{page_count}"
    f_sub = im.font(S(14))
    d.text((right - im.text_w(d, sub, f_sub), S(64)), sub, font=f_sub, fill=im.MUTED)
    d.line([(S(margin), S(head_h - 12)), (right, S(head_h - 12))], fill=im.GRID, width=max(1, S(1)))

    # ---- table header
    y0 = head_h
    x_days = margin + name_w
    x_stats = x_days + n * pitch
    f_h = im.font(S(12), True)
    d.text((S(margin), S(y0 + 20)), "PARTICIPANT", font=f_h, fill=im.MUTED)
    f_day, f_wd = im.font(S(13), True), im.font(S(9))
    for i, day in enumerate(days):
        cx = S(x_days + i * pitch + cell / 2)
        label = str(day.day)
        d.text((cx - im.text_w(d, label, f_day) / 2, S(y0 + 14)), label, font=f_day, fill=im.WHITE)
        wd = "MTWTFSS"[day.weekday()]
        d.text((cx - im.text_w(d, wd, f_wd) / 2, S(y0 + 34)), wd, font=f_wd, fill=im.MUTED)
    for j, label in enumerate(STAT_COLS):
        cx = S(x_stats + j * stat_w + stat_w / 2)
        d.text((cx - im.text_w(d, label, f_h) / 2, S(y0 + 20)), label, font=f_h, fill=im.MUTED)
    d.line([(S(margin), S(y0 + th_h - 4)), (right, S(y0 + th_h - 4))], fill=im.GRID, width=max(1, S(1)))

    # ---- rows
    f_name, f_val = im.font(S(15), True), im.font(S(15))
    today_note = None
    for r, s in enumerate(rows):
        y = y0 + th_h + r * row_h
        if r % 2 == 0:
            d.rectangle([S(margin - 8), S(y), right + S(8), S(y + row_h)], fill=im.PANEL_ALT)
        d.text((S(margin), S(y + row_h / 2)), im.fit(d, s.name, f_name, S(name_w - 12)),
               font=f_name, fill=im.WHITE, anchor="lm")
        for i, day in enumerate(days):
            rec = s.days[day]
            x = x_days + i * pitch
            box = [S(x), S(y + (row_h - cell) / 2), S(x + cell), S(y + (row_h + cell) / 2)]
            im.draw_status_square(d, box, rec.status, k, dim=rec.future)
        vals = [f"{s.verified_minutes / 60:.1f}", str(s.gross_points), f"-{s.penalty_total}" if s.penalty_total else "0",
                str(s.net_points), str(s.current_streak)]
        colors = [im.WHITE, im.WHITE, im.RED if s.penalty_total else im.MUTED, im.LIME, im.WHITE]
        for j, (v, c) in enumerate(zip(vals, colors)):
            d.text((S(x_stats + j * stat_w + stat_w / 2), S(y + row_h / 2)), v, font=f_val if j != 3 else f_name,
                   fill=c, anchor="mm")
    if not rows:
        d.text((S(margin), S(y0 + th_h + 10)), "No participants enrolled yet.", font=f_val, fill=im.MUTED)
    # subtle week separators
    for i, day in enumerate(days):
        if day.weekday() == 0 and i > 0:
            x = S(x_days + i * pitch - 2)
            d.line([(x, S(y0 + 8)), (x, S(y0 + th_h + max(1, len(rows)) * row_h))], fill=im.GRID, width=max(1, S(1)))

    # ---- legend
    ly = y0 + th_h + max(1, len(rows)) * row_h + 26
    f_leg = im.font(S(13))
    hours = session["full_green_minutes"] / 60
    items = [(scoring.GREEN, f"Full green (≥ {hours:g}h, tolerance {session['tolerance_minutes']}m)"),
             (scoring.HALF, "Partial"), (scoring.RED, "Failed day"), (scoring.PENDING, "Pending approval"),
             (scoring.HEALTH, "Health"), (scoring.EMPTY, "No record")]
    x = margin
    for st, text in items:
        im.draw_status_square(d, [S(x), S(ly), S(x + 18), S(ly + 18)], st, k)
        d.text((S(x + 26), S(ly + 9)), text, font=f_leg, fill=im.MUTED, anchor="lm")
        x += 26 + im.text_w(d, text, f_leg) / k + 26
    d.text((S(margin), S(height - 24)),
           f"Generated {generated_at.strftime('%d %b %Y, %H:%M')} IST  ·  {reports.session_title(session)}",
           font=im.font(S(12)), fill=im.MUTED, anchor="lm")
    return img
